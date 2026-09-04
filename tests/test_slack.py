"""Slack: тред інциденту, довгі звіти, і те, що збій каналу не губить звіт."""
import json

import pytest


@pytest.fixture
def slack_api(monkeypatch, tmp_path):
    """Підміняє єдиний мережевий виклик; повертає лог payload-ів і керовану відповідь."""
    from agents.tools import slack

    calls, state = [], {"response": {"ok": True, "ts": "1700000000.1", "channel": "C1"}}

    def fake_call(payload, url=None):
        calls.append(payload)
        return state["response"]

    monkeypatch.setattr(slack, "_call", fake_call)
    monkeypatch.setattr(slack, "SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack, "SLACK_CHANNEL", "#sre-agent")
    monkeypatch.setattr(slack, "THREAD_MAP", tmp_path / "map.json")
    return type("S", (), {"calls": calls, "state": state})()


def test_first_message_opens_thread_and_rest_go_into_it(slack_api):
    from agents.tools import slack

    first = slack.post("alert-abc", "перше: алерт")
    assert "thread_ts" not in slack_api.calls[0], "перше повідомлення створює тред"
    assert first["ts"] == "1700000000.1"

    slack.post("alert-abc", "друге: звіт RCA")
    assert slack_api.calls[1]["thread_ts"] == "1700000000.1", \
        "звіт має лягти в тред алерту, а не окремим повідомленням"


def test_different_alerts_get_different_threads(slack_api):
    from agents.tools import slack

    slack.post("alert-one", "a")
    slack_api.state["response"] = {"ok": True, "ts": "1700000009.9", "channel": "C1"}
    slack.post("alert-two", "b")
    slack.post("alert-one", "a2")

    assert slack_api.calls[2]["thread_ts"] == "1700000000.1", "тред першого алерту"


def test_thread_map_survives_restart(slack_api, tmp_path):
    from agents.tools import slack

    slack.post("alert-abc", "перше")
    saved = json.loads((tmp_path / "map.json").read_text())
    assert saved["alert-abc"] == "1700000000.1", "мапа тредів має лежати на диску"


def test_long_report_is_split_into_sections(slack_api):
    from agents.tools import slack

    slack.post("alert-abc", "рядок звіту\n" * 900)
    blocks = slack_api.calls[0]["blocks"]
    assert len(blocks) > 1, "довгий звіт має розбитись на секції"
    assert all(len(b["text"]["text"]) <= slack.MAX_SECTION_CHARS for b in blocks)


def test_slack_ok_false_is_treated_as_failure(slack_api):
    """Slack віддає HTTP 200 з ok=false — без перевірки це мовчазна втрата звіту."""
    from agents.tools import slack

    slack_api.state["response"] = {"ok": False, "error": "not_in_channel"}
    result = slack.post("alert-abc", "звіт")

    assert result["error"] == "slack: not_in_channel"
    assert "invite" in result["hint"], "підказка має казати, що робити"


def test_report_is_not_lost_when_slack_fails(monkeypatch, tmp_path, slack_api):
    """Канал доставки менш важливий за сам звіт."""
    import agents.tools.actions as actions

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "threads.json")
    slack_api.state["response"] = {"ok": False, "error": "channel_not_found"}

    result = actions.post_slack.invoke({"thread_id": "alert-abc", "text": "важливий звіт"})

    assert result["transport"] == "file", "звіт мав осісти у файл"
    assert result["slack_error"] == "slack: channel_not_found"
    assert "важливий звіт" in (tmp_path / "threads.json").read_text()


def test_without_token_everything_goes_to_file(monkeypatch, tmp_path):
    import agents.tools.actions as actions
    from agents.tools import slack

    monkeypatch.setattr(slack, "SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "threads.json")

    result = actions.post_slack.invoke({"thread_id": "t1", "text": "привіт"})
    assert result["transport"] == "file" and result["messages_in_thread"] == 1


@pytest.mark.parametrize("token, expected", [
    ("", "порожній"),
    ("xoxb-твій-токен", "нелатинські"),
    ("xapp-123456789", "має починатись з xoxb-"),
    ("xoxb-1234-5678-abcdef", ""),
])
def test_token_problems_are_named_before_the_request(token, expected):
    """Кирилиця в токені валить urllib стектрейсом про latin-1 — це не діагноз."""
    from agents.tools import slack

    problem = slack.check_token(token, "xoxb-", "SLACK_BOT_TOKEN")
    assert expected in problem if expected else problem == ""


def test_incident_lookup_by_slack_thread(slack_api):
    from agents.tools import slack

    slack.remember_thread("incident-abc-2026-09-03T20:00:00", "1788465633.877759")
    slack.remember_thread("1788465633.877759", "1788465633.877759")  # самопосилання від згадки

    found = slack.incident_for_thread("1788465633.877759")
    assert found == "incident-abc-2026-09-03T20:00:00", \
        "самопосилання не має перемагати справжній інцидент"
    assert slack.incident_for_thread("невідомий-ts") is None


def test_update_rewrites_the_same_message(slack_api):
    from agents.tools import slack

    posted = slack.post("alert-abc", "перша редакція звіту")
    slack.update(posted["channel"], posted["ts"], "звіт з вердиктом критика")

    call = slack_api.calls[-1]
    assert call["ts"] == posted["ts"], "має редагуватись те саме повідомлення"
    assert "вердиктом" in call["blocks"][0]["text"]["text"]


def test_update_reports_failure_with_a_hint(slack_api):
    from agents.tools import slack

    posted = slack.post("alert-abc", "звіт")
    slack_api.state["response"] = {"ok": False, "error": "message_not_found"}
    result = slack.update(posted["channel"], posted["ts"], "нова редакція")
    assert result["error"] == "slack: message_not_found"
