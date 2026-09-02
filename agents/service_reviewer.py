"""A3 · Service Reviewer — ревізія сервісу і пропозиція алертів.

Чекліст детермінований і живе в коді, а не в промпті: оцінка "у вас немає алерту на
latency" не має залежати від настрою моделі. LLM тут не потрібен узагалі — на виході
скоркарт і готовий YAML правил.

MVP-обріз: логування + алерти. Дашборди і ресурси — roadmap (див. README).
"""
from __future__ import annotations

import json
import re

import yaml

from agents.tools.catalog import get_service
from agents.tools.observability import _fetch_logs
from agents.tools.stand import get_alert_rules

GOLDEN_SIGNALS = ("error_rate", "latency", "restarts", "saturation")
SIGNAL_MARKERS = {
    "error_rate": ('status=~"5', "errors_total"),
    "latency": ("histogram_quantile", "duration_seconds"),
    "restarts": ("process_start_time", "restarts_total", "container_restart"),
    "saturation": ("resident_memory", "cpu_usage", "memory_working_set"),
}
# tier -> скільки часу алерт має протриматись, перш ніж будити людину
FOR_BY_TIER = {1: "30s", 2: "2m", 3: "5m"}
SEVERITY_BY_TIER = {1: "critical", 2: "warning", 3: "warning"}

PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\b\d{1,3}(\.\d{1,3}){3}\b|\bsk-[A-Za-z0-9]{8,}")
GRADES = ((0.9, "A"), (0.75, "B"), (0.6, "C"), (0.4, "D"), (0.0, "F"))
SAMPLE_SIZE = 200  # вибірка рядків для оцінки якості логів


def _grade(score: float) -> str:
    return next(letter for threshold, letter in GRADES if score >= threshold)


def check_logging(service: str, minutes: int = 60) -> dict:
    """Чи придатні логи для розслідування: структура, рівні, trace_id, відсутність PII.

    Сирі рядки, а не патерни: query_loki_patterns навмисно вирізає msg і викидає
    структуру запису — тобто саме те, що тут оцінюється. Context-Minimization захищає
    контекст моделі, а цей чекліст — детермінований Python, і йому потрібен оригінал.
    """
    lines = _fetch_logs(f'{{service="{service}"}}', minutes, SAMPLE_SIZE)
    if not lines:
        return {"section": "logging", "grade": "F", "score": 0.0,
                "findings": ["логів за вікно немає — розслідувати інцидент буде нічим"]}

    parsed = [json.loads(line) for line in lines if _is_json(line)]
    structured = len(parsed) / len(lines)
    with_level = sum("level" in p for p in parsed) / len(lines) if parsed else 0.0
    with_trace = sum("trace_id" in p for p in parsed) / len(lines) if parsed else 0.0
    leaking = [l for l in lines if PII.search(l)]

    checks = {
        "структуровані JSON-логи": structured >= 0.8,
        "рівень логування в кожному записі": with_level >= 0.8,
        "trace_id для звʼязку записів": with_trace >= 0.5,
        "немає PII/секретів у логах": not leaking,
    }
    findings = [f"{name}: ні" for name, passed in checks.items() if not passed]
    if leaking:
        findings.append(f"знайдено PII у {len(leaking)} зразках — маскувати до запису")

    score = sum(checks.values()) / len(checks)
    return {"section": "logging", "grade": _grade(score), "score": round(score, 2),
            "findings": findings, "metrics": {"structured": round(structured, 2),
                                              "with_trace_id": round(with_trace, 2)}}


def _is_json(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except json.JSONDecodeError:
        return False


def check_alerts(service: str) -> dict:
    """Чи покриті golden signals алертами і чи ведуть вони до runbook."""
    rules = get_alert_rules.invoke({"service": service})
    if rules and "error" in rules[0]:
        return {"section": "alerts", "grade": "F", "score": 0.0, "findings": [rules[0]["error"]]}

    covered = {
        signal for signal in GOLDEN_SIGNALS
        for rule in rules if any(marker in rule["expr"] for marker in SIGNAL_MARKERS[signal])
    }
    missing = [s for s in GOLDEN_SIGNALS if s not in covered]
    without_runbook = [r["name"] for r in rules if not r.get("runbook_url")]
    without_for = [r["name"] for r in rules if not r.get("for")]

    findings = []
    if missing:
        findings.append(f"немає алертів на: {', '.join(missing)}")
    if without_runbook:
        findings.append(f"без runbook_url: {', '.join(without_runbook)}")
    if without_for:
        findings.append(f"без for (спрацюють на одиничному викиді): {', '.join(without_for)}")

    score = len(covered) / len(GOLDEN_SIGNALS) * (0.7 if without_runbook else 1.0)
    return {"section": "alerts", "grade": _grade(score), "score": round(score, 2),
            "findings": findings, "missing_signals": missing}


def propose_alert_rules(service: str, missing: list[str]) -> str:
    """Готовий YAML правил для відсутніх сигналів — з for, severity по tier і runbook."""
    card = get_service.invoke({"name": service})
    tier = card.get("tier", 3) if "error" not in card else 3
    runbook = card.get("runbook", "kb/runbooks/high-error-rate.md")

    templates = {
        "error_rate": (f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m]))'
                       f' / clamp_min(sum(rate(http_requests_total{{service="{service}"}}[1m])), 0.001) > 0.05',
                       "error rate вище 5%"),
        "latency": (f'histogram_quantile(0.95, sum by (le) '
                    f'(rate(http_request_duration_seconds_bucket{{service="{service}"}}[2m]))) '
                    f'> {card.get("slo", {}).get("latency_p95_ms", 500) / 1000}',
                    "p95 вище SLO"),
        "restarts": (f'changes(process_start_time_seconds{{service="{service}"}}[10m]) > 2',
                     "більше 2 рестартів за 10 хвилин"),
        "saturation": (f'process_resident_memory_bytes{{service="{service}"}} > 180e6',
                       "пам'ять близько до ліміту"),
    }
    rules = [
        {
            "alert": f"{service.replace('-', '')}{signal.title().replace('_', '')}",
            "expr": expression,
            "for": FOR_BY_TIER[tier],
            "labels": {"severity": SEVERITY_BY_TIER[tier], "service": service,
                       "tier": str(tier)},
            "annotations": {"summary": f"{service}: {description}", "runbook_url": runbook},
        }
        for signal in missing if (entry := templates.get(signal))
        for expression, description in [entry]
    ]
    return yaml.safe_dump({"groups": [{"name": f"{service}-golden-signals", "rules": rules}]},
                          allow_unicode=True, sort_keys=False)


def review(service: str) -> dict:
    """Скоркарт сервісу плюс артефакт: YAML правил для непокритих сигналів."""
    sections = [check_logging(service), check_alerts(service)]
    average = sum(s["score"] for s in sections) / len(sections)
    missing = next((s.get("missing_signals", []) for s in sections if s["section"] == "alerts"), [])
    return {
        "service": service,
        "overall_grade": _grade(average),
        "sections": sections,
        "proposed_alert_rules": propose_alert_rules(service, missing) if missing else "",
    }
