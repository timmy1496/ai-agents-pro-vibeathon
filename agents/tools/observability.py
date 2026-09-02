"""Тули метрик і логів: прямі HTTP-запити до Prometheus і Loki.

Чому не Grafana MCP: у промпт агента не можна класти сирі відповіді — 500 лог-рядків
це і дорогий контекст, і untrusted text. Тому кожен тул тут стискає вивід до фактів
(агрегати замість рядів, патерни замість рядків) ще до того, як його побачить модель.
Grafana MCP лишається для annotations / alert rules / panel images.
"""
from __future__ import annotations

import collections
import json
import re
import time
import urllib.parse
import urllib.request

from langchain_core.tools import tool

from agents.config import LOKI_URL, PROMETHEUS_URL

MAX_LOG_LINES = 2000     # стеля вибірки з Loki; у промпт іде агрегат, не ці рядки
MAX_SAMPLES = 3          # скільки прикладів рядків на патерн віддаємо
HTTP_TIMEOUT = 10

# Нормалізація рядка в патерн: числа, id, ip і лапки — це змінна частина повідомлення.
NOISE = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+(:\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|MB|GB|%)?\b"), "<num>"),
    (re.compile(r'"[^"]*"'), '"<str>"'),
]


def _get(url: str, params: dict) -> dict:
    """Один вихід у мережу на весь модуль — тести підміняють саме його."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read())


def _window(minutes: int) -> tuple[int, int]:
    now = int(time.time())
    return now - minutes * 60, now


def _series_summary(result: list[dict]) -> list[dict]:
    """Ряд точок → мін/макс/середнє/останнє. Модель не вміє читати 120 точок, і не мусить."""
    summaries = []
    for series in result:
        values = [float(v) for _, v in series.get("values", []) if v not in ("NaN", "+Inf")]
        if not values:
            continue
        summaries.append({
            "labels": {k: v for k, v in series["metric"].items() if k != "__name__"},
            "min": round(min(values), 4), "max": round(max(values), 4),
            "avg": round(sum(values) / len(values), 4), "last": round(values[-1], 4),
            "points": len(values),
        })
    return summaries


@tool
def query_prometheus(query: str, minutes: int = 60, step: str = "30s") -> dict:
    """Довільний PromQL за вікно в N хвилин. Повертає агрегати ряду (min/max/avg/last).

    Приклади:
      sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))
      histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[2m])))
    """
    start, end = _window(minutes)
    payload = _get(f"{PROMETHEUS_URL}/api/v1/query_range",
                   {"query": query, "start": start, "end": end, "step": step})
    if payload.get("status") != "success":
        return {"error": payload.get("error", "prometheus query failed"), "query": query}
    return {"query": query, "window_minutes": minutes,
            "series": _series_summary(payload["data"]["result"])}


@tool
def golden_signals(service: str, minutes: int = 30, baseline_offset_minutes: int = 60) -> dict:
    """Golden signals сервісу за вікно ПЛЮС той самий зріз годину тому для порівняння.

    Один виклик замість чотирьох: error rate, p95, RPS, рестарти, пам'ять — і дельта
    до baseline. Абсолютні числа без baseline нічого не кажуть, тому вони тут разом.
    """
    queries = {
        "error_rate": f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m]))'
                      f' / clamp_min(sum(rate(http_requests_total{{service="{service}"}}[1m])), 0.001)',
        "latency_p95": f'histogram_quantile(0.95, sum by (le) '
                       f'(rate(http_request_duration_seconds_bucket{{service="{service}"}}[2m])))',
        "rps": f'sum(rate(http_requests_total{{service="{service}"}}[1m]))',
        "restarts": f'changes(process_start_time_seconds{{service="{service}"}}[10m])',
        "memory_bytes": f'process_resident_memory_bytes{{service="{service}"}}',
    }
    signals = {}
    for name, promql in queries.items():
        now = query_prometheus.func(promql, minutes=minutes)
        before = query_prometheus.func(promql, minutes=minutes + baseline_offset_minutes)
        current = now["series"][0] if now.get("series") else None
        baseline = before["series"][0] if before.get("series") else None
        signals[name] = {
            "current_avg": current["avg"] if current else None,
            "current_max": current["max"] if current else None,
            "baseline_avg": baseline["avg"] if baseline else None,
            "promql": promql,
        }
    return {"service": service, "window_minutes": minutes, "signals": signals}


def _pattern(line: str) -> str:
    for regex, placeholder in NOISE:
        line = regex.sub(placeholder, line)
    return line.strip()[:200]


def _fetch_logs(selector: str, minutes: int, limit: int) -> list[str]:
    start, end = _window(minutes)
    payload = _get(f"{LOKI_URL}/loki/api/v1/query_range", {
        "query": selector, "start": start * 10**9, "end": end * 10**9,
        "limit": limit, "direction": "backward",
    })
    if payload.get("status") != "success":
        return []
    return [line for stream in payload["data"]["result"] for _, line in stream["values"]]


@tool
def query_loki_patterns(service: str, minutes: int = 30, level: str = "error",
                        top_n: int = 5) -> dict:
    """Топ-N патернів помилок сервісу за вікно, з лічильником і 3 прикладами на патерн.

    Це основний тул для логів. Сирі рядки в промпт не йдуть: вони і дорогі, і є
    недовіреним текстом — по них можна прилетіти prompt injection.
    """
    selector = f'{{service="{service}"}} | json | level="{level}"' if level \
        else f'{{service="{service}"}}'
    lines = _fetch_logs(selector, minutes, MAX_LOG_LINES)

    groups: dict[str, list[str]] = collections.defaultdict(list)
    for line in lines:
        message = line
        try:  # логи стенду структуровані — беремо поле msg, а не весь JSON
            parsed = json.loads(line)
            message = parsed.get("msg", line) if isinstance(parsed, dict) else line
        except (json.JSONDecodeError, TypeError):
            pass  # неструктурований рядок беремо як є
        groups[_pattern(message)].append(message)

    top = collections.Counter({p: len(v) for p, v in groups.items()}).most_common(top_n)
    return {
        "service": service, "window_minutes": minutes, "level": level,
        "total_lines": len(lines), "distinct_patterns": len(groups),
        "patterns": [
            {"pattern": pattern, "count": count, "samples": groups[pattern][:MAX_SAMPLES]}
            for pattern, count in top
        ],
    }


@tool
def query_loki_logs(service: str, contains: str, minutes: int = 30, limit: int = 20) -> dict:
    """Точковий пошук рядків із підрядком — коли патернів мало і треба глянути конкретику."""
    lines = _fetch_logs(f'{{service="{service}"}} |= `{contains}`', minutes, limit)
    return {"service": service, "contains": contains,
            "matched": len(lines), "lines": lines[:limit]}
