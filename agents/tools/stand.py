"""Тули змін і подій: деплої, події кластера, активні алерти.

Деплої і події кластера читаються з data/*.json. Kubernetes на стенді не піднімаємо —
сигнатура тулу така сама, якою була б поверх kind, тож заміна джерела нічого не ламає.
"""
from __future__ import annotations

import datetime as dt
import json

from langchain_core.tools import tool

from agents.config import ALERTMANAGER_URL, DATA_DIR
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
