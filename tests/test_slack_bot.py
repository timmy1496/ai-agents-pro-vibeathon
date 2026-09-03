"""Обробка подій Slack: тредінг, ігнорування власних повідомлень, зняття згадки."""
import pytest


class FakeSupervisor:
    def __init__(self, answer="відповідь агента"):
        self.answer = answer
        self.seen = []

    def invoke(self, state, config=None):
        self.seen.append((state["messages"][-1]["content"], config))
        return {"messages": [type("M", (), {"content": self.answer})()]}


@pytest.fixture
def posted(monkeypatch, tmp_path):
    from agents.tools import slack

    monkeypatch.setattr(slack, "THREAD_MAP", tmp_path / "map.json")
    sent = []
    return sent, (lambda **kwargs: sent.append(kwargs))


def test_mention_in_channel_starts_a_thread_on_that_message(posted):
    from agents.slack_bot import handle_event

    sent, post = posted
    supervisor = FakeSupervisor()
    handle_event({"type": "app_mention", "text": "<@U1> що з demo-chaos-svc?",
                  "channel": "C1", "ts": "1700.1"}, supervisor, post)

    assert [m["thread_ts"] for m in sent] == ["1700.1", "1700.1"], \
        "і 'розбираюсь', і відповідь мають лягти в тред самої згадки"
    assert sent[-1]["text"] == "відповідь агента"


def test_reply_inside_thread_stays_in_that_thread(posted):
    from agents.slack_bot import handle_event

    sent, post = posted
    handle_event({"type": "message", "text": "<@U1> а деталі?", "channel": "C1",
                  "ts": "1700.9", "thread_ts": "1700.1"}, FakeSupervisor(), post)

    assert all(m["thread_ts"] == "1700.1" for m in sent), "відповідь не має вилазити в канал"


def test_thread_id_for_the_agent_is_the_slack_thread(posted):
    """Пам'ять діалогу і тред у Slack — це одне й те саме."""
    from agents.slack_bot import handle_event

    _, post = posted
    supervisor = FakeSupervisor()
    handle_event({"type": "app_mention", "text": "<@U1> питання", "channel": "C1",
                  "ts": "1700.1"}, supervisor, post)

    _, config = supervisor.seen[0]
    assert config["configurable"]["thread_id"] == "1700.1"


def test_mention_is_stripped_before_the_model_sees_it(posted):
    from agents.slack_bot import handle_event

    _, post = posted
    supervisor = FakeSupervisor()
    handle_event({"type": "app_mention", "text": "<@U0BUABC> хто власник cart-service?",
                  "channel": "C1", "ts": "1700.1"}, supervisor, post)

    question, _ = supervisor.seen[0]
    assert question == "хто власник cart-service?"


@pytest.mark.parametrize("event", [
    {"bot_id": "B1", "text": "<@U1> привіт", "channel": "C1", "ts": "1.1"},
    {"subtype": "message_changed", "text": "<@U1> привіт", "channel": "C1", "ts": "1.1"},
    {"text": "<@U1>", "channel": "C1", "ts": "1.1"},
])
def test_events_that_must_be_ignored(posted, event):
    """Власні повідомлення бота — пряма дорога в нескінченний цикл."""
    from agents.slack_bot import handle_event

    sent, post = posted
    supervisor = FakeSupervisor()
    assert handle_event(event, supervisor, post) is None
    assert not sent and not supervisor.seen
