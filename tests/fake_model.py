"""Підставна чат-модель зі сценарієм відповідей.

Потрібна, щоб ганяти граф агента в CI без провайдера і без грошей: перевіряємо
проводку (чи прив'язались тули, чи агент їх викликав, чи результат повернувся
в стан), а не якість формулювань — це вже робота LLM-judge на релізі.
"""
from __future__ import annotations

from typing import Any, Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """Віддає наперед задані AIMessage по черзі. bind_tools лише запам'ятовує тули."""

    script: list[AIMessage]
    bound_tools: list[Any] = []
    calls: list[list] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "ScriptedChatModel":
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages: list, stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        step = min(len(self.calls) - 1, len(self.script) - 1)
        return ChatResult(generations=[ChatGeneration(message=self.script[step])])

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator:
        raise NotImplementedError("сценарій не стрімиться — тестам це не потрібно")
