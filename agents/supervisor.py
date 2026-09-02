"""A0 · Supervisor — єдина точка входу.

Класифікує намір і маршрутизує на воркера. Стан діалогу тримає checkpointer,
де thread_id = ідентифікатор Slack-треда: продовження розмови в тому самому треді
бачить попередні кроки без жодного коду з нашого боку.

Роутер — окремий дешевий виклик, а не «нехай сильна модель сама розбереться»:
класифікація на шість класів не потребує Sonnet, а помилка тут дешева й видима.
"""
from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from agents.config import CHEAP_MODEL
from agents.models import resolve

Intent = Literal["ALERT", "RCA", "KB", "REVIEW", "RELEASE", "HUMAN"]

ROUTER_PROMPT = """Класифікуй запит SRE-інженера в один з намірів:

ALERT   — прийшов алерт або просять розібратись з поточним інцидентом
RCA     — просять знайти корене­ву причину чогось, що вже сталося
KB      — питання по базі знань: постмортеми, runbooks, хто власник, що таке X
REVIEW  — просять зробити ревізію сервісу, оцінити алерти/логування/ресурси
RELEASE — просять перевірити метрики після релізу або стежити за викатом
HUMAN   — незрозуміло, або потрібне рішення людини

Витягни назву сервісу, якщо вона є в запиті. Немає — залиш порожнім."""


class Route(BaseModel):
    intent: Intent
    service: str = Field(default="", description="Назва сервісу або порожньо")


class SupervisorState(MessagesState):
    intent: str
    service: str


NOT_IMPLEMENTED = {
    "REVIEW": "Ревізія сервісу (A3) ще не реалізована — у roadmap.",
    "RELEASE": "Моніторинг релізу (A4) ще не реалізований — у roadmap.",
}


def _last_user_text(state: SupervisorState) -> str:
    return next(str(m.content) for m in reversed(state["messages"]) if m.type == "human")


def route(state: SupervisorState) -> dict:
    router = resolve(CHEAP_MODEL).with_structured_output(Route)
    decision = router.invoke([
        {"role": "system", "content": ROUTER_PROMPT},
        {"role": "user", "content": _last_user_text(state)},
    ])
    return {"intent": decision.intent, "service": decision.service}


def knowledge_node(state: SupervisorState) -> dict:
    from agents.knowledge_agent import build_agent

    result = build_agent().invoke({"messages": state["messages"]})
    return {"messages": [result["messages"][-1]]}


def incident_node(state: SupervisorState) -> dict:
    from agents.incident_agent import investigate

    outcome = investigate({"summary": _last_user_text(state), "service": state["service"]})
    if outcome["report"] is None:
        return {"messages": [{"role": "assistant",
                              "content": f"Звіт не завершено: {outcome['error']}"}]}
    return {"messages": [{"role": "assistant", "content": render_report(outcome)}]}


def human_node(state: SupervisorState) -> dict:
    message = NOT_IMPLEMENTED.get(state["intent"],
                                  "Не зрозумів запит. Уточни сервіс і що саме перевірити.")
    return {"messages": [{"role": "assistant", "content": message}]}


def render_report(outcome: dict) -> str:
    """Звіт у вигляді, придатному для Slack-треда."""
    report = outcome["report"]
    lines = [
        f"*{report.service}* — гіпотеза: {report.hypothesis}",
        f"Клас причини: `{report.root_cause_label}`, впевненість {report.confidence:.0%}",
        "",
        "*Докази:*",
        *[f"• {e.fact}\n  _{e.source}_" for e in report.evidence],
        "",
        "*Рекомендовані дії:*",
        *[f"{i}. {a}" for i, a in enumerate(report.recommended_actions, 1)],
    ]
    if report.similar_incidents:
        lines += ["", "*Схожі інциденти:* " + ", ".join(report.similar_incidents)]
    lines += ["", f"_критик: {'ok' if outcome['verdict'].grounded else 'зауваження'}, "
                  f"доопрацювань: {outcome['revisions']}_"]
    return "\n".join(lines)


def _next_node(state: SupervisorState) -> str:
    return {"ALERT": "incident", "RCA": "incident", "KB": "knowledge"}.get(state["intent"], "human")


def build_supervisor(checkpointer=None):
    graph = StateGraph(SupervisorState)
    graph.add_node("route", route)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("incident", incident_node)
    graph.add_node("human", human_node)

    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", _next_node, ["knowledge", "incident", "human"])
    for node in ("knowledge", "incident", "human"):
        graph.add_edge(node, END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Хто власник payment-gateway?"
    app = build_supervisor()
    state = app.invoke({"messages": [{"role": "user", "content": question}]},
                       config={"configurable": {"thread_id": "cli"}})
    print(f"[намір: {state['intent']}, сервіс: {state['service'] or '—'}]\n")
    print(state["messages"][-1].content)
