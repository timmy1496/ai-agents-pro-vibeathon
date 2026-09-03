"""PreToolUse-хук: деструктивна дія не доходить ані до тула, ані до людини.

Чому це окремий middleware, а не перевірка всередині `propose_action`.

`HumanInTheLoopMiddleware` зупиняє граф у своєму `after_model` — тобто ПЕРЕД вузлом
тулів. Тіло тула виконується вже після «ок» людини, тому регекс усередині тула фізично
не може відсіяти `kubectl delete` раніше, ніж людина його побачить. Порядок був зворотним
і до брифу (§6: PreToolUse-хук), і до README. Тепер відсів живе там, де й має: між
моделлю і будь-яким споживачем її tool_call.

Механіка — дві половини одного правила, обидві читають той самий `is_destructive`:

1. `DestructiveActionGuard.after_model` віддає на деструктивний виклик ToolMessage зі
   status="error". LangGraph рахує pending лише ті tool_calls, у яких ще немає
   ToolMessage (`factory.py`, `pending_tool_calls`), тому тул не виконається взагалі.
2. `when` у конфізі HITL повертає False на тому самому предикаті, тому HITL на цей
   виклик не переривається і людина його не бачить.

Порядок middleware у списку має значення і він зворотний до інтуїції: LangChain
зшиває `after_model`-хуки з кінця списку до початку (`graph.add_edge("model",
middleware_w_after_model[-1])`), тож щоб guard відпрацював ПЕРШИМ, він стоїть
у списку ОСТАННІМ. На це є тест — test_guard_runs_before_the_human_sees_the_call.
"""
from __future__ import annotations

import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolMessage

# Аналог PreToolUse-deny з брифу: список того, що агент не пропонує ніколи, незалежно
# від того, наскільки переконливо він це обґрунтував.
DESTRUCTIVE = re.compile(
    r"\b(kubectl\s+delete|drop\s+(table|database)|rm\s+-rf|truncate\s+table"
    r"|delete\s+from|helm\s+uninstall|terraform\s+destroy|--force\s+--grace-period=0)\b",
    re.IGNORECASE,
)

BLOCK_REASON = "деструктивна команда заборонена політикою"

# Аргументи, у яких може приїхати команда. Перевіряємо всі: модель кладе команду
# то в command, то в текст дії — і в обох випадках це та сама небезпека.
COMMAND_ARGS = ("command", "action")

# Тули, чиї аргументи проходять через фільтр. Read-тули сюди не входять свідомо:
# у них немає аргументу, який виконується.
GUARDED_TOOLS = ("propose_action",)


def is_destructive(args: dict[str, Any]) -> bool:
    """Чи містять аргументи tool_call команду зі списку заборонених."""
    return any(DESTRUCTIVE.search(str(args.get(key, ""))) for key in COMMAND_ARGS)


def _blocked_message(tool_call: dict) -> ToolMessage:
    return ToolMessage(
        content=f"ЗАБЛОКОВАНО ПОЛІТИКОЮ: {BLOCK_REASON}. Команда "
                f"{tool_call['args'].get('command') or tool_call['args'].get('action')!r} "
                f"не виконана і людині не показана. Запропонуй недеструктивну "
                f"альтернативу або опиши дію словами в recommended_actions.",
        name=tool_call["name"],
        tool_call_id=tool_call["id"],
        status="error",
    )


class DestructiveActionGuard(AgentMiddleware):
    """Відсікає деструктивні tool_call'и одразу після моделі — до HITL і до вузла тулів."""

    def after_model(self, state, runtime) -> dict | None:  # noqa: ARG002 — контракт хука
        messages = state["messages"]
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if not last_ai or not last_ai.tool_calls:
            return None

        blocked = [
            call for call in last_ai.tool_calls
            if call["name"] in GUARDED_TOOLS and is_destructive(call["args"])
        ]
        if not blocked:
            return None
        # tool_call лишається в AIMessage навмисно: ToolMessage без пари ламає формат
        # запиту до Anthropic. Виконання зупиняє саме наявність відповіді на виклик.
        return {"messages": [_blocked_message(call) for call in blocked]}


def _human_reviews(request) -> bool:
    """Чи показувати цей виклик людині. Деструктив не показуємо — його вже відхилено."""
    return not is_destructive(request.tool_call["args"])


def approval_middleware() -> list[AgentMiddleware]:
    """HITL + guard у правильному порядку. Разом — одна політика, порізно вони неповні."""
    return [
        HumanInTheLoopMiddleware(interrupt_on={
            "propose_action": {
                "allowed_decisions": ["approve", "reject"],
                "when": _human_reviews,
            },
        }),
        # ОСТАННІЙ у списку = ПЕРШИЙ у виконанні after_model (див. docstring модуля)
        DestructiveActionGuard(),
    ]
