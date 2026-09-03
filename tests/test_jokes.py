"""Жарт у тред: доречний до алерту, не повторюється підряд, вимикається."""
import pytest


@pytest.fixture(autouse=True)
def jokes_on(monkeypatch):
    from agents import jokes

    monkeypatch.setattr(jokes, "JOKES_ENABLED", True)
    jokes._last.clear()


@pytest.mark.parametrize("alertname", ["HighErrorRate", "HighLatencyP95",
                                       "FrequentRestarts", "HighMemoryUsage"])
def test_joke_matches_the_alert_type(alertname):
    from agents import jokes

    joke = jokes.pick(alertname, "t1")
    assert any(joke.endswith(option) for option in jokes.BY_ALERT[alertname]), \
        "жарт має бути з набору саме цього алерту"


def test_unknown_alert_falls_back_to_generic():
    from agents import jokes

    joke = jokes.pick("ЩосьНезнайоме", "t1")
    assert any(joke.endswith(option) for option in jokes.GENERIC)


def test_same_joke_does_not_repeat_in_a_row():
    from agents import jokes

    previous = None
    for _ in range(12):
        current = jokes.pick("HighErrorRate", "t1")
        assert current != previous, "повтор підряд у тому самому треді"
        previous = current


def test_threads_do_not_share_the_no_repeat_memory():
    from agents import jokes

    jokes.pick("HighErrorRate", "t1")
    assert jokes.pick("HighErrorRate", "t2") is not None


def test_disabled_by_env(monkeypatch):
    """Під час справжнього інциденту гумор доречний не завжди."""
    from agents import jokes

    monkeypatch.setattr(jokes, "JOKES_ENABLED", False)
    assert jokes.pick("HighErrorRate", "t1") is None


def test_no_joke_mentions_users_or_customers():
    """Жарти — про моніторинг і про нас, ніколи про тих, кому зараз погано."""
    from agents import jokes

    everything = sum(jokes.BY_ALERT.values(), []) + jokes.GENERIC
    forbidden = ("клієнт", "користувач постраждал", "втрат", "гроші")
    for joke in everything:
        assert not any(word in joke.lower() for word in forbidden), joke


def test_incident_flow_does_not_joke_on_its_own(monkeypatch, tmp_path):
    """Під час падіння бот не підкидає гумор сам — тільки на прямий запит."""
    import json

    import agents.app as app_module
    import agents.tools.actions as actions

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")
    monkeypatch.setattr(app_module, "_handle",
                        lambda text, thread_id: actions.post_slack.invoke(
                            {"thread_id": thread_id, "text": "звіт RCA"}))

    app_module._process_alert("HighErrorRate critical", "t1")

    thread = json.loads((tmp_path / "slack.json").read_text())["t1"]
    assert len(thread) == 2, "має бути лише алерт і звіт"
    assert not any(":coffee:" in m["text"] for m in thread)


def test_joke_node_answers_on_request():
    from langchain_core.messages import HumanMessage

    from agents.supervisor import joke_node

    state = {"messages": [HumanMessage(content="пожартуй")], "service": "", "intent": "JOKE"}
    answer = joke_node(state)["messages"][0]["content"]
    assert answer.startswith(":coffee:")


def test_joke_in_an_incident_thread_matches_that_alert():
    """Якщо просять у треді інциденту — жарт до того типу алерту, що там розбирався."""
    from langchain_core.messages import AIMessage, HumanMessage

    from agents import jokes
    from agents.supervisor import joke_node

    state = {"messages": [
        HumanMessage(content="HighMemoryUsage warning на demo-chaos-svc"),
        AIMessage(content="Клас причини: `resources`"),
        HumanMessage(content="пожартуй")], "service": "demo-chaos-svc", "intent": "JOKE"}

    answer = joke_node(state)["messages"][0]["content"]
    assert any(answer.endswith(option) for option in jokes.BY_ALERT["HighMemoryUsage"])


def test_joke_node_says_so_when_humour_is_off(monkeypatch):
    from langchain_core.messages import HumanMessage

    from agents import jokes
    from agents.supervisor import joke_node

    monkeypatch.setattr(jokes, "JOKES_ENABLED", False)
    state = {"messages": [HumanMessage(content="пожартуй")], "service": "", "intent": "JOKE"}
    assert "вимкнено" in joke_node(state)["messages"][0]["content"]
