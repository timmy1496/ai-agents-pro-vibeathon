"""Сервіс-каталог як інструменти. Файл маленький і статичний — читаємо один раз у пам'ять."""
from __future__ import annotations

import functools

import yaml
from langchain_core.tools import tool

from agents.config import CATALOG_FILE


@functools.cache
def _services() -> dict[str, dict]:
    data = yaml.safe_load(CATALOG_FILE.read_text(encoding="utf-8"))
    return {svc["name"]: svc for svc in data["services"]}


@tool
def get_service(name: str) -> dict:
    """Картка сервісу з каталогу: tier, власник, залежності, runbook, SLO, дашборди.

    Викликай першим для будь-якого сервісу в алерті — tier визначає терміновість,
    deps звужують пошук причини, runbook дає перевірені дії.
    """
    service = _services().get(name)
    if not service:
        return {"error": f"сервіс '{name}' відсутній у каталозі",
                "known_services": sorted(_services())}
    return service


@tool
def list_services(tier: int | None = None) -> list[dict]:
    """Список сервісів каталогу (name, tier, owner, deps). Опційно — лише вказаний tier."""
    return [
        {"name": s["name"], "tier": s["tier"], "owner": s["owner"], "deps": s["deps"]}
        for s in _services().values() if tier is None or s["tier"] == tier
    ]
