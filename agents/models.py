"""Один резолвер моделі на весь проєкт.

Ключ вирішує, куди йти: sk-or-... це OpenRouter (OpenAI-сумісний протокол),
решта — Anthropic напряму. Агенти про це не знають і приймають рядок як раніше.
"""
from __future__ import annotations

import functools

from langchain_core.language_models import BaseChatModel

from agents.config import OPENROUTER_BASE_URL, OPENROUTER_KEY


@functools.cache
def _build(model: str, temperature: float) -> BaseChatModel:
    if OPENROUTER_KEY:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=OPENROUTER_KEY,
                          base_url=OPENROUTER_BASE_URL, temperature=temperature)

    from langchain.chat_models import init_chat_model

    return init_chat_model(model, temperature=temperature)


def resolve(model: str | BaseChatModel, temperature: float = 0.0) -> BaseChatModel:
    """Рядок -> клієнт потрібного провайдера. Готову модель (тести) віддає як є."""
    return _build(model, temperature) if isinstance(model, str) else model
