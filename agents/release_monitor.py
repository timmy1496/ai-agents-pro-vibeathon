"""A4 · Release Monitor — метрики після релізу.

Це workflow, а не агент: порівняння вікон детерміноване і живе в коді, LLM робить рівно
один крок — формулює вердикт для Slack. Рішення "чи відкочувати" ухвалює людина.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.config import CHEAP_MODEL
from agents.models import resolve
from agents.tools.catalog import get_service
from agents.tools.observability import golden_signals

Status = Literal["healthy", "degraded", "rollback_recommended"]

# Поріг деградації залежить від tier: те, що для tier-3 шум, для tier-1 інцидент.
# Значення — у відносному прирості до baseline, крім рестартів (абсолютна кількість).
THRESHOLDS = {
    1: {"error_rate": 2.0, "latency_p95": 1.5, "restarts": 1},
    2: {"error_rate": 3.0, "latency_p95": 2.0, "restarts": 2},
    3: {"error_rate": 5.0, "latency_p95": 3.0, "restarts": 3},
}


class ReleaseVerdict(BaseModel):
    status: Status
    summary: str = Field(description="Одне-два речення для Slack-треда")


def _ratio(current: float | None, baseline: float | None) -> float | None:
    """У скільки разів гірше за baseline. None, якщо метрики немає."""
    if current is None or baseline is None:
        return None
    if baseline == 0:
        return float("inf") if current > 0 else 1.0
    return round(current / baseline, 2)


def compare(service: str, window_minutes: int = 15) -> dict:
    """Порівнює вікно після деплою з baseline і каже, які пороги пробито.

    Порогова частина навмисно без LLM: вона має бути відтворюваною і придатною
    для тестів, інакше «сервіс здоровий» стає питанням настрою моделі.
    """
    card = get_service.invoke({"name": service})
    tier = card.get("tier", 3) if "error" not in card else 3
    limits = THRESHOLDS[tier]

    signals = golden_signals.invoke({"service": service, "minutes": window_minutes})["signals"]
    deltas, breached = {}, []
    for name, limit in limits.items():
        current = signals[name]["current_max" if name == "restarts" else "current_avg"]
        baseline = signals[name]["baseline_avg"]
        if name == "restarts":
            deltas[name] = {"current": current, "limit": limit}
            if (current or 0) > limit:
                breached.append(name)
            continue
        ratio = _ratio(current, baseline)
        deltas[name] = {"current": current, "baseline": baseline, "ratio": ratio, "limit": limit}
        if ratio is not None and ratio > limit:
            breached.append(name)

    return {"service": service, "tier": tier, "window_minutes": window_minutes,
            "deltas": deltas, "breached": breached,
            "status": _status(breached, tier)}


def _status(breached: list[str], tier: int) -> Status:
    """Для tier-1 будь-яке пробиття порогу — привід до відкату; далі шкала м'якша."""
    if not breached:
        return "healthy"
    if tier == 1 or len(breached) >= 2:
        return "rollback_recommended"
    return "degraded"


VERDICT_PROMPT = """Ти формулюєш короткий вердикт для Slack після релізу.
Статус уже порахований детерміновано — не змінюй його, поясни його числами.
Одне-два речення, українською, без води. Якщо статус rollback_recommended — скажи прямо,
що рекомендується відкат, і на підставі яких метрик."""


def monitor(service: str, window_minutes: int = 15, model: str = CHEAP_MODEL) -> dict:
    """Повний крок: порівняння + формулювання. LLM тут один виклик і не вирішує статус."""
    comparison = compare(service, window_minutes)
    writer = resolve(model).with_structured_output(ReleaseVerdict)
    verdict = writer.invoke([
        {"role": "system", "content": VERDICT_PROMPT},
        {"role": "user", "content": f"Порахований статус: {comparison['status']}\n"
                                    f"Пробиті пороги: {comparison['breached'] or 'немає'}\n"
                                    f"Метрики: {comparison['deltas']}"},
    ])
    # статус лишається детермінованим навіть якщо модель напише щось інше
    return {**comparison, "summary": verdict.summary}
