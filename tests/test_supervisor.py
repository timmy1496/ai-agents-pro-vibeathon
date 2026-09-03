"""A0: маршрутизація намірів і пам'ять треда. Модель роутера підставна."""
import pytest

from tests.fake_model import ScriptedChatModel


@pytest.fixture
def routed(monkeypatch):
    """Підміняє роутер так, щоб він повертав заданий намір, і глушить воркерів."""
    import agents.supervisor as supervisor

    seen = {}

    def make(intent, service=""):
        def fake_route(state):
            seen["text"] = supervisor._last_user_text(state)
            return {"intent": intent, "service": service}

        monkeypatch.setattr(supervisor, "route", fake_route)
        for name in ("knowledge_node", "incident_node", "review_node", "release_node"):
            monkeypatch.setattr(supervisor, name,
                                lambda state, n=name: {"messages": [
                                    {"role": "assistant", "content": f"викликано {n}"}]})
        return supervisor.build_supervisor()

    return type("R", (), {"make": staticmethod(make), "seen": seen})()


@pytest.mark.parametrize("intent, expected", [
    ("ALERT", "incident_node"),
    ("RCA", "incident_node"),
    ("KB", "knowledge_node"),
])
def test_intent_reaches_the_right_worker(routed, intent, expected):
    app = routed.make(intent)
    state = app.invoke({"messages": [{"role": "user", "content": "щось сталось"}]},
                       config={"configurable": {"thread_id": "t1"}})
    assert expected in state["messages"][-1].content


@pytest.mark.parametrize("intent, expected", [
    ("REVIEW", "review_node"),
    ("RELEASE", "release_node"),
])
def test_review_and_release_reach_their_workers(routed, intent, expected):
    app = routed.make(intent, service="demo-chaos-svc")
    state = app.invoke({"messages": [{"role": "user", "content": "перевір сервіс"}]},
                       config={"configurable": {"thread_id": f"t-{intent}"}})
    assert expected in state["messages"][-1].content


def test_unknown_intent_asks_for_clarification(routed):
    app = routed.make("HUMAN")
    state = app.invoke({"messages": [{"role": "user", "content": "щось"}]},
                       config={"configurable": {"thread_id": "t-human"}})
    assert "Уточни" in state["messages"][-1].content


def test_thread_keeps_history_between_calls(routed):
    """thread_id = Slack-тред: друге питання бачить перше."""
    app = routed.make("KB")
    config = {"configurable": {"thread_id": "slack-C123-1700"}}
    app.invoke({"messages": [{"role": "user", "content": "перше питання"}]}, config=config)
    state = app.invoke({"messages": [{"role": "user", "content": "друге питання"}]}, config=config)

    human = [m.content for m in state["messages"] if m.type == "human"]
    assert human == ["перше питання", "друге питання"]
    assert routed.seen["text"] == "друге питання", "роутер має дивитись на останнє питання"


def test_router_prompt_covers_every_intent():
    from agents.supervisor import ROUTER_PROMPT, Intent

    for intent in Intent.__args__:
        assert intent in ROUTER_PROMPT, f"намір {intent} не описаний у промпті роутера"


def test_report_renders_evidence_and_sources():
    from agents.incident_agent import Evidence, RCAReport, Verdict
    from agents.supervisor import render_report

    report = RCAReport(
        service="demo-chaos-svc", root_cause_label="release",
        hypothesis="Регресія у v1.5.0", confidence=0.86,
        evidence=[Evidence(fact="error rate 34% проти 0.1%", source="PromQL: sum(rate(...))")],
        recommended_actions=["Відкотити на 1.4.2"],
        similar_incidents=["kb/postmortems/2025-10-21-demo-chaos-svc-release-5xx.md"])
    rendered = render_report({"report": report, "revisions": 0,
                              "verdict": Verdict(grounded=True, verdict="ACCEPT")})

    assert "error rate 34%" in rendered and "PromQL" in rendered, "докази без джерел марні"
    assert "86%" in rendered and "release" in rendered
    assert "2025-10-21" in rendered


def test_followup_goes_to_knowledge_not_a_new_investigation(routed):
    """Уточнююче питання у треді зі звітом не має запускати RCA наново."""
    app = routed.make("FOLLOWUP", service="demo-chaos-svc")
    state = app.invoke({"messages": [{"role": "user", "content": "а чи було таке раніше?"}]},
                       config={"configurable": {"thread_id": "t-followup"}})
    assert "knowledge_node" in state["messages"][-1].content


def test_router_is_told_whether_the_thread_already_has_a_report(monkeypatch):
    """Роутер не вгадує з тексту: йому прямо кажуть, чи є вже розслідування."""
    import agents.supervisor as supervisor
    from langchain_core.messages import AIMessage, HumanMessage

    seen = {}

    class FakeRouter:
        def invoke(self, messages):
            seen["prompt"] = messages[-1]["content"]
            return supervisor.Route(intent="FOLLOWUP", service="demo-chaos-svc")

    monkeypatch.setattr(supervisor, "resolve",
                        lambda *a, **k: type("M", (), {"with_structured_output":
                                                       lambda self, schema: FakeRouter()})())

    with_report = {"messages": [
        HumanMessage(content="алерт"),
        AIMessage(content="*demo-chaos-svc* — гіпотеза: реліз\nКлас причини: `release`"),
        HumanMessage(content="а чи було таке раніше?")]}
    supervisor.route(with_report)
    assert "ВЖЕ Є звіт" in seen["prompt"]

    without = {"messages": [HumanMessage(content="розберись з алертом")]}
    supervisor.route(without)
    assert "звіту ще немає" in seen["prompt"]


def test_report_marker_matches_what_render_report_produces():
    """Якщо формат звіту зміниться, роутер осліпне — тримаємо це тестом."""
    from agents.incident_agent import Evidence, RCAReport, Verdict
    from agents.supervisor import REPORT_MARKER, render_report

    rendered = render_report({
        "report": RCAReport(service="s", root_cause_label="release", hypothesis="h",
                            confidence=0.9, evidence=[Evidence(fact="f", source="s")],
                            recommended_actions=["a"]),
        "revisions": 0, "verdict": Verdict(grounded=True, verdict="ACCEPT")})
    assert REPORT_MARKER in rendered
