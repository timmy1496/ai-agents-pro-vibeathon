"""Один резолвер моделі на весь проєкт.

Провайдер обирається за тим, що взагалі доступно, і порядок тут не випадковий:

1. **OpenRouter** (`sk-or-...`) — OpenAI-сумісний протокол, інший клієнт;
2. **Anthropic напряму** (`ANTHROPIC_API_KEY`) — те, на чому працює прод;
3. **Claude Code CLI** (`claude -p`) — підписка замість ключа. Транспорт для евалів:
   агентський цикл, middleware і структурований вивід лишаються ті самі, але tool
   calling стає промптовим, а не нативним. Це інший вимірювальний інструмент, тому
   провайдер потрапляє в meta звіту і дельта між провайдерами не рахується.

Явно попросити конкретний транспорт: `SRE_MODEL_PROVIDER=claude-code|api`.
Агенти про це не знають і приймають рядок як раніше.
"""
from __future__ import annotations

import functools
import os

from langchain_core.language_models import BaseChatModel

from agents.config import OPENROUTER_BASE_URL, OPENROUTER_KEY

PROVIDER_OVERRIDE = os.getenv("SRE_MODEL_PROVIDER", "").strip().lower()


def provider() -> str:
    """Яким транспортом підуть виклики. Читається в звіті евалів, тому це не деталь."""
    if PROVIDER_OVERRIDE:
        return PROVIDER_OVERRIDE
    if OPENROUTER_KEY:
        return "openrouter"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic-api"

    from agents.providers import claude_code

    return "claude-code" if claude_code.available() else "anthropic-api"


@functools.cache
def _build(model: str, temperature: float) -> BaseChatModel:
    chosen = provider()

    if chosen == "claude-code":
        from agents.providers import claude_code

        built = claude_code.build(model, temperature)
        _TRACKED.append(built)
        return built

    if chosen == "openrouter":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=OPENROUTER_KEY,
                          base_url=OPENROUTER_BASE_URL, temperature=temperature)

    from langchain.chat_models import init_chat_model

    return init_chat_model(model, temperature=temperature)


def resolve(model: str | BaseChatModel, temperature: float = 0.0) -> BaseChatModel:
    """Рядок -> клієнт потрібного провайдера. Готову модель (тести) віддає як є."""
    return _build(model, temperature) if isinstance(model, str) else model


# Побудовані моделі, що самі рахують витрати. functools.cache не віддає своїх значень,
# тому реєстр окремий. Прив'язані копії (bind_tools) ділять словник spend з батьком,
# тому рахуємо словники за identity, а не моделі.
_TRACKED: list[BaseChatModel] = []


def spend() -> dict | None:
    """Скільки коштували виклики, якщо провайдер це рахує.

    На підписці це не рахунок, а порядок величини за прейскурантом — але саме він
    відповідає на питання «скільки коштує розібраний інцидент», яке бриф називає KPI.
    """
    if not _TRACKED:
        return None
    totals = {"usd": 0.0, "calls": 0, "input": 0, "output": 0}
    counted: set[int] = set()
    for model in _TRACKED:
        if id(model.spend) in counted:
            continue
        counted.add(id(model.spend))
        for key in totals:
            totals[key] += model.spend[key]
    return totals


def reset_spend() -> None:
    """Обнулити лічильник — між прогонами він не має накопичуватись."""
    for model in _TRACKED:
        model.spend.update(usd=0.0, calls=0, input=0, output=0)
