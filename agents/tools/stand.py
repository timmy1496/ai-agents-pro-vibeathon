"""Тули змін і подій: деплої, події кластера, активні алерти.

Деплої і події кластера читаються з data/*.json. Kubernetes на стенді не піднімаємо —
сигнатура тулу така сама, якою була б поверх kind, тож заміна джерела нічого не ламає.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from langchain_core.tools import tool

from agents.config import ALERTMANAGER_URL, DATA_DIR, PROMETHEUS_URL
from agents.tools.observability import _get


def _recent(filename: str, service: str | None, hours: int) -> list[dict]:
    path = DATA_DIR / filename
    if not path.exists():
        return []
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    events = [
        e for e in json.loads(path.read_text())
        if dt.datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) >= cutoff
        and (service is None or e["service"] == service)
    ]
    return sorted(events, key=lambda e: e["ts"], reverse=True)


@tool
def get_deploys(service: str | None = None, hours: int = 6) -> list[dict]:
    """Деплої за останні N годин: коли, яка версія, хто, який коміт.

    Викликай завжди при сплеску 5xx або latency: збіг початку інциденту з деплоєм
    у межах 5 хвилин — найсильніший доказ причини, який взагалі буває.
    """
    return _recent("deploys.json", service, hours)


@tool
def k8s_events(service: str | None = None, hours: int = 6) -> list[dict]:
    """Події кластера: OOMKilling, BackOff, Unhealthy, Created, Killing.

    Warning-події тут відрізняють проблему ресурсів (OOMKilling) від проблеми
    готовності (Unhealthy) — на метриках це виглядає однаково.
    """
    return _recent("k8s_events.json", service, hours)


@tool
def get_active_alerts(service: str | None = None) -> list[dict]:
    """Активні алерти з Alertmanager: назва, severity, мітки, з якого часу горить."""
    try:
        alerts = _get(f"{ALERTMANAGER_URL}/api/v2/alerts", {"active": "true", "silenced": "false"})
    except OSError as error:
        return [{"error": f"alertmanager недоступний: {error}"}]

    return [
        {
            "alertname": alert["labels"].get("alertname"),
            "service": alert["labels"].get("service"),
            "severity": alert["labels"].get("severity"),
            "summary": alert["annotations"].get("summary"),
            "runbook_url": alert["annotations"].get("runbook_url"),
            "started_at": alert.get("startsAt"),
        }
        for alert in alerts
        if service is None or alert["labels"].get("service") == service
    ]


# Прив'язка правила до сервісу в самому виразі: service="x" або service=~"x|y".
SERVICE_SELECTOR = re.compile(r'\bservice\s*(=~|=)\s*"([^"]*)"')


def _applies_to(expr: str, service: str) -> bool:
    """Чи стосується правило цього сервісу.

    Раніше тут було `service in rule["query"]`, і це відповідало на інше питання:
    «чи згадано ім'я сервісу у виразі». Реальні правила golden-signals пишуть один раз
    на весь стенд і агрегують `by (service)` — імені сервісу в них немає взагалі, тому
    ревізія здорового сервісу бачила нуль правил і ставила F за повної відсутності
    проблеми. Правило без прив'язки покриває ВСІ сервіси — зокрема й цей.
    """
    pinned = SERVICE_SELECTOR.findall(expr)
    if not pinned:
        return True
    return any(_matches(operator, value, service) for operator, value in pinned)


def _matches(operator: str, value: str, service: str) -> bool:
    if operator == "=":
        return value == service
    try:  # =~ це RE2 у Prometheus; несумісний із re синтаксис не має валити ревізію
        return re.fullmatch(value, service) is not None
    except re.error:
        return False


@tool
def get_alert_rules(service: str | None = None) -> list[dict]:
    """Правила алертів з Prometheus: вираз, for, severity, чи є runbook_url.

    Потрібен для ревізії: покриття golden signals і наявність runbook у мітках —
    саме те, чого зазвичай бракує, і саме те, що видно лише з правил, а не з метрик.

    service фільтрує за тим, чи правило СТОСУЄТЬСЯ сервісу, а не за згадкою його імені:
    сервіс-агностичне правило (агрегація `by (service)` без селектора) покриває і його.
    """
    try:
        payload = _get(f"{PROMETHEUS_URL}/api/v1/rules", {"type": "alert"})
    except OSError as error:
        return [{"error": f"prometheus недоступний: {error}"}]

    return [
        {
            "name": rule["name"],
            "expr": rule["query"],
            "for": rule.get("duration", 0),
            "severity": rule.get("labels", {}).get("severity"),
            "runbook_url": rule.get("annotations", {}).get("runbook_url"),
            "group": group["name"],
        }
        for group in payload.get("data", {}).get("groups", [])
        for rule in group.get("rules", [])
        if rule.get("type") == "alerting"
        and (service is None or _applies_to(rule.get("query", ""), service))
    ]
