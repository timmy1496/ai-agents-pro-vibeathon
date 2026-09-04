"""Тули метрик і логів: прямі HTTP-запити до Prometheus і Loki.

Чому не Grafana MCP: у промпт агента не можна класти сирі відповіді — 500 лог-рядків
це і дорогий контекст, і untrusted text. Тому кожен тул тут стискає вивід до фактів
(агрегати замість рядів, патерни замість рядків) ще до того, як його побачить модель.
Grafana MCP лишається для annotations / alert rules / panel images.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import time
import urllib.parse
import urllib.request

from langchain_core.tools import tool

from agents.config import LOKI_URL, PROMETHEUS_URL

log = logging.getLogger(__name__)

MAX_LOG_LINES = 2000     # стеля вибірки з Loki; у промпт іде агрегат, не ці рядки
MAX_SAMPLES = 3          # скільки прикладів рядків на патерн віддаємо
HTTP_TIMEOUT = 30  # Loki по 2000 рядках буває повільним під паралельними розслідуваннями

# Селектори збирає модель, яка щойно прочитала недовірені логи, тому все, що приходить
# від неї, потрапляє в запит лише через ці дві функції.
#
# Ім'я сервісу — не рядок, а ідентифікатор: валідуємо за алфавітом і не екрануємо взагалі
# (екранування дало б працездатний запит з неочікуваним значенням, а тут потрібна відмова).
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,62}$")


class UnsafeSelector(ValueError):
    """Аргумент не проходить у селектор — запит не будується взагалі."""


def _label(service: str) -> str:
    """Ім'я сервісу, придатне для мітки селектора. Інакше — відмова, не санітизація."""
    if not SERVICE_NAME.match(service or ""):
        raise UnsafeSelector(f"недопустиме ім'я сервісу: {service!r}")
    return service


def _literal(value: str) -> str:
    r"""Рядковий літерал LogQL у подвійних лапках з Go-екрануванням.

    Backtick-літерал (`|= \`...\``) екранування не має в принципі: перший же backtick
    у значенні закриває рядок, а далі йде вже синтаксис запиту. Тому підрядок для
    пошуку йде тільки сюди.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r"))
    return f'"{escaped}"'


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


def _safe_get(url: str, params: dict) -> dict | None:
    """Те саме, але недоступне джерело — це не привід валити все розслідування.

    Тул, що кинув виняток, обриває цикл агента і лишає інцидент без звіту. Краще
    повернути порожньо і сказати про це в іншому полі: агент побачить брак даних
    і зробить чесний висновок "unknown", а не помре на півдорозі.
    """
    try:
        return _get(url, params)
    except (OSError, json.JSONDecodeError) as error:
        log.warning("джерело недоступне: %s (%s)", url, error)
        return None


def _window(minutes: int, ends_minutes_ago: int = 0) -> tuple[int, int]:
    """Вікно завдовжки `minutes`, що закінчується `ends_minutes_ago` хвилин тому.

    Другий аргумент і є те, що робить baseline справжнім baseline: без нього
    «вікно до інциденту» вийшло б вікном, яке інцидент у себе включає.
    """
    now = int(time.time())
    end = now - ends_minutes_ago * 60
    return end - minutes * 60, end


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
def query_prometheus(query: str, minutes: int = 60, step: str = "30s",
                     ends_minutes_ago: int = 0) -> dict:
    """Довільний PromQL за вікно в N хвилин. Повертає агрегати ряду (min/max/avg/last).

    ends_minutes_ago зсуває вікно в минуле: 0 — до «зараз», 30 — вікно, що закінчилось
    пів години тому. Так беруть зріз ДО інциденту, не зачепивши сам інцидент.

    Приклади:
      sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))
      histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[2m])))
    """
    start, end = _window(minutes, ends_minutes_ago)
    payload = _safe_get(f"{PROMETHEUS_URL}/api/v1/query_range",
                        {"query": query, "start": start, "end": end, "step": step})
    if payload is None:
        return {"error": "prometheus недоступний", "query": query, "series": []}
    if payload.get("status") != "success":
        return {"error": payload.get("error", "prometheus query failed"), "query": query}
    return {"query": query, "window_minutes": minutes,
            "ends_minutes_ago": ends_minutes_ago,
            "series": _series_summary(payload["data"]["result"])}


@tool
def golden_signals(service: str, minutes: int = 30, baseline_minutes: int = 60) -> dict:
    """Golden signals сервісу за вікно ПЛЮС той самий зріз ДО нього для порівняння.

    Один виклик замість чотирьох: error rate, p95, RPS, рестарти, пам'ять — і дельта
    до baseline. Абсолютні числа без baseline нічого не кажуть, тому вони тут разом.

    Два вікна не перетинаються, і це принципово (бриф A4: baseline = година до деплою,
    window = 15 хв після):
        current  = [now - minutes, now]
        baseline = [now - minutes - baseline_minutes, now - minutes]
    Раніше baseline брався ширшим вікном від «зараз» — тобто включав у себе сам
    інцидент і тягнув середнє вгору. Ratio виходив систематично заниженим, і A4 на
    tier-3 (поріг error_rate 5.0) промахувався рівно на порозі: чиста деградація
    0 -> 0.5 давала рівно 5.0 і порогу не пробивала.
    """
    service = _label(service)
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
        before = query_prometheus.func(promql, minutes=baseline_minutes,
                                       ends_minutes_ago=minutes)
        current = now["series"][0] if now.get("series") else None
        baseline = before["series"][0] if before.get("series") else None
        signals[name] = {
            "current_avg": current["avg"] if current else None,
            "current_max": current["max"] if current else None,
            "baseline_avg": baseline["avg"] if baseline else None,
            "promql": promql,
        }
    return {"service": service, "window_minutes": minutes,
            "baseline_minutes": baseline_minutes, "signals": signals}


def _pattern(line: str) -> str:
    for regex, placeholder in NOISE:
        line = regex.sub(placeholder, line)
    return line.strip()[:200]


def _fetch_logs(selector: str, minutes: int, limit: int) -> list[str]:
    start, end = _window(minutes)
    payload = _safe_get(f"{LOKI_URL}/loki/api/v1/query_range", {
        "query": selector, "start": start * 10**9, "end": end * 10**9,
        "limit": limit, "direction": "backward",
    })
    if payload is None or payload.get("status") != "success":
        return []
    return [line for stream in payload["data"]["result"] for _, line in stream["values"]]


@tool
def query_loki_patterns(service: str, minutes: int = 30, level: str = "error",
                        top_n: int = 5) -> dict:
    """Топ-N патернів помилок сервісу за вікно, з лічильником і 3 прикладами на патерн.

    Це основний тул для логів. Сирі рядки в промпт не йдуть: вони і дорогі, і є
    недовіреним текстом — по них можна прилетіти prompt injection.
    """
    selector = f'{{service="{_label(service)}"}}'
    if level:
        selector += f" | json | level={_literal(level)}"
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
    lines = _fetch_logs(f'{{service="{_label(service)}"}} |= {_literal(contains)}',
                        minutes, limit)
    return {"service": service, "contains": contains,
            "matched": len(lines), "lines": lines[:limit]}


# Що саме композит покриває. Оголошено поруч із ним навмисно: цей список читають
# евали, щоб не вимагати окремого виклику тула, дані якого вже приїхали разом.
# Міряти імена викликаних тулів замість покриття доказами — помилка вимірювання:
# агент, який зібрав ті самі дані одним викликом замість чотирьох, зробив краще,
# а не гірше.
COMPOSES = {
    "incident_snapshot": ("get_service", "golden_signals", "query_loki_patterns",
                          "get_deploys", "k8s_events"),
}


@tool
def incident_snapshot(service: str, minutes: int = 30) -> dict:
    """Усе, з чого починається розслідування, за один виклик: картка сервісу,
    golden signals з baseline, топ-патерни помилок, деплої і події кластера.

    Викликай ЦЕ першим замість чотирьох окремих тулів. Кожен виклик тулу — це ще один
    обіг до моделі, а вони й складають майже весь час розслідування.
    """
    from agents.tools.catalog import get_service
    from agents.tools.stand import get_deploys, k8s_events

    return {
        "service_card": get_service.invoke({"name": service}),
        "signals": golden_signals.invoke({"service": service, "minutes": minutes})["signals"],
        "log_patterns": query_loki_patterns.invoke(
            {"service": service, "minutes": minutes})["patterns"],
        "deploys": get_deploys.invoke({"service": service, "hours": 6}),
        "k8s_events": k8s_events.invoke({"service": service, "hours": 6}),
    }
