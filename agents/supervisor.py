"""A0 · Supervisor — єдина точка входу.

Класифікує намір і маршрутизує на воркера. Стан діалогу тримає checkpointer,
де thread_id = ідентифікатор Slack-треда: продовження розмови в тому самому треді
бачить попередні кроки без жодного коду з нашого боку.

Роутер — окремий дешевий виклик, а не «нехай сильна модель сама розбереться»:
класифікація на шість класів не потребує Sonnet, а помилка тут дешева й видима.
"""
from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field

from agents.checkpoint import saver
from agents.config import CHEAP_MODEL
from agents.models import resolve

Intent = Literal["ALERT", "RCA", "FOLLOWUP", "KB", "REVIEW", "RELEASE", "JOKE", "HUMAN"]

# Маркер, за яким видно, що в цьому треді вже є розслідування. Потрібен роутеру:
# уточнююче питання у треді зі звітом — це FOLLOWUP, а не привід розслідувати наново.
REPORT_MARKER = "Клас причини:"

ROUTER_PROMPT = """Класифікуй запит SRE-інженера в один з намірів:

ALERT   — прийшов алерт або просять розібратись з НОВИМ інцидентом
RCA     — просять знайти корене­ву причину чогось, що вже сталося
FOLLOWUP — уточнююче питання про інцидент, який у цьому треді ВЖЕ розібрано:
          "а чи було таке раніше?", "що з залежностями?", "покажи runbook", "а деталі?".
          Обирай FOLLOWUP щоразу, коли у треді вже є звіт, а людина просто питає далі.
          ALERT тут доречний лише якщо просять розслідувати заново або явно кажуть,
          що ситуація змінилась.
KB      — питання по базі знань: постмортеми, runbooks, хто власник, що таке X
REVIEW  — просять зробити ревізію сервісу, оцінити алерти/логування/ресурси
RELEASE — просять перевірити метрики після релізу або стежити за викатом
JOKE    — просять пожартувати або розповісти анекдот: "пожартуй", "анекдот",
          "розваж", "щось смішне". Тільки коли просять прямо.
HUMAN   — незрозуміло, або потрібне рішення людини

Витягни назву сервісу, якщо вона є в запиті. Немає — залиш порожнім."""


class Route(BaseModel):
    intent: Intent
    service: str = Field(default="", description="Назва сервісу або порожньо")


class SupervisorState(MessagesState):
    intent: str
    service: str


NOT_IMPLEMENTED: dict[str, str] = {}


def _last_user_text(state: SupervisorState) -> str:
    return next(str(m.content) for m in reversed(state["messages"]) if m.type == "human")


def _thread_has_report(state: SupervisorState) -> bool:
    return any(REPORT_MARKER in str(m.content) for m in state["messages"] if m.type == "ai")


def route(state: SupervisorState) -> dict:
    router = resolve(CHEAP_MODEL).with_structured_output(Route)
    context = ("У цьому треді ВЖЕ Є звіт RCA. Якщо це не прохання розслідувати заново — "
               "це FOLLOWUP." if _thread_has_report(state) else
               "У цьому треді звіту ще немає.")
    decision = router.invoke([
        {"role": "system", "content": ROUTER_PROMPT},
        {"role": "user", "content": f"{context}\n\nЗапит: {_last_user_text(state)}"},
    ])
    return {"intent": decision.intent, "service": decision.service}


def knowledge_node(state: SupervisorState) -> dict:
    from agents.knowledge_agent import build_agent

    result = build_agent().invoke({"messages": state["messages"]})
    return {"messages": [result["messages"][-1]]}


def incident_node(state: SupervisorState, on_report=None) -> dict:
    from agents.incident_agent import investigate

    outcome = investigate({"summary": _last_user_text(state), "service": state["service"]},
                          on_report=on_report)
    if outcome["report"] is None:
        return {"messages": [{"role": "assistant",
                              "content": f"Звіт не завершено: {outcome['error']}"}]}
    return {"messages": [{"role": "assistant", "content": render_report(outcome)}]}


def review_node(state: SupervisorState) -> dict:
    from agents.service_reviewer import review

    if not state["service"]:
        return {"messages": [{"role": "assistant", "content": "Вкажи сервіс: /sre review <service>"}]}
    return {"messages": [{"role": "assistant", "content": render_review(review(state["service"]))}]}


def release_node(state: SupervisorState) -> dict:
    from agents.release_monitor import monitor

    if not state["service"]:
        return {"messages": [{"role": "assistant", "content": "Вкажи сервіс: /sre release <service>"}]}
    result = monitor(state["service"])
    icon = {"healthy": ":white_check_mark:", "degraded": ":warning:",
            "rollback_recommended": ":rotating_light:"}[result["status"]]
    return {"messages": [{"role": "assistant", "content":
        f"{icon} *{result['service']}* (tier {result['tier']}): `{result['status']}`\n"
        f"{result['summary']}\n"
        f"Пробиті пороги: {', '.join(result['breached']) or 'немає'}"}]}


def render_review(result: dict) -> str:
    lines = [f"*Ревізія {result['service']}* — загальна оцінка `{result['overall_grade']}`", ""]
    for section in result["sections"]:
        lines.append(f"*{section['section']}*: `{section['grade']}`")
        lines += [f"  • {finding}" for finding in section["findings"]] or ["  • зауважень немає"]
    if result["proposed_alert_rules"]:
        lines += ["", "*Пропоновані правила алертів:*", "```", result["proposed_alert_rules"], "```"]
    return "\n".join(lines)


def joke_node(state: SupervisorState) -> dict:
    """Жарт на запит. У треді інциденту — до того типу алерту, що там розбирався."""
    from agents import jokes

    history = " ".join(str(m.content) for m in state["messages"])
    alertname = next((name for name in jokes.BY_ALERT if name in history), "")
    joke = jokes.pick(alertname, state.get("service", ""))
    return {"messages": [{"role": "assistant",
                          "content": joke or "Гумор вимкнено (SRE_JOKES=0)."}]}


def human_node(state: SupervisorState) -> dict:
    return {"messages": [{"role": "assistant",
                          "content": "Не зрозумів запит. Уточни сервіс і що саме перевірити."}]}


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
    if outcome.get("verdict") is not None:
        lines += ["", f"_критик: {'ok' if outcome['verdict'].grounded else 'зауваження'}, "
                      f"доопрацювань: {outcome['revisions']}_"]
    return "\n".join(lines)


def _next_node(state: SupervisorState) -> str:
    # FOLLOWUP іде в knowledge: у нього є і база знань, і вся історія треда,
    # тому він відповідає на уточнення, не переробляючи розслідування з нуля.
    return {"ALERT": "incident", "RCA": "incident",
            "FOLLOWUP": "knowledge", "KB": "knowledge",
            "REVIEW": "review", "RELEASE": "release",
            "JOKE": "joke"}.get(state["intent"], "human")


def build_supervisor(checkpointer=None):
    graph = StateGraph(SupervisorState)
    graph.add_node("route", route)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("incident", incident_node)
    graph.add_node("joke", joke_node)
    graph.add_node("review", review_node)
    graph.add_node("release", release_node)
    graph.add_node("human", human_node)

    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", _next_node,
                                ["knowledge", "incident", "review", "release",
                                 "joke", "human"])
    for node in ("knowledge", "incident", "review", "release", "joke", "human"):
        graph.add_edge(node, END)

    return graph.compile(checkpointer=checkpointer or saver())


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Хто власник payment-gateway?"
    app = build_supervisor()
    state = app.invoke({"messages": [{"role": "user", "content": question}]},
                       config={"configurable": {"thread_id": "cli"}})
    print(f"[намір: {state['intent']}, сервіс: {state['service'] or '—'}]\n")
    print(state["messages"][-1].content)
