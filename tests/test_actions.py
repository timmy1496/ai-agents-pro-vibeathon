"""Write-тули: єдиний вихід агента назовні, тому перевіряємо саме межі."""
import json

import pytest


@pytest.mark.parametrize("command", [
    "kubectl delete pod chaos-svc-abc",
    "kubectl delete deploy/x --force --grace-period=0",
    "DROP TABLE orders;",
    "helm uninstall payment-gateway",
    "rm -rf /var/lib/data",
    "DELETE FROM users WHERE 1=1",
    "terraform destroy",
])
def test_destructive_commands_never_reach_a_human(command):
    from agents.tools.actions import propose_action

    result = propose_action.invoke({"service": "demo-chaos-svc", "action": "fix",
                                    "reason": "бо треба", "command": command})
    assert result["blocked"] is True, command


@pytest.mark.parametrize("command", [
    "kubectl rollout undo deploy/demo-chaos-svc",
    "kubectl scale deploy/notification-worker --replicas=20",
    "helm upgrade demo-chaos-svc ./chart --set image.tag=1.4.2",
])
def test_safe_remediation_is_proposed_not_executed(command):
    from agents.tools.actions import propose_action

    result = propose_action.invoke({"service": "demo-chaos-svc", "action": "rollback",
                                    "reason": "регресія релізу", "command": command})
    assert result["status"] == "awaiting_human_approval", "агент не виконує, а пропонує"
    assert "blocked" not in result


def test_post_slack_appends_to_thread(tmp_path, monkeypatch):
    import agents.tools.actions as actions

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack_threads.json")
    actions.post_slack.invoke({"thread_id": "T1", "text": "перше"})
    result = actions.post_slack.invoke({"thread_id": "T1", "text": "друге"})

    assert result["messages_in_thread"] == 2
    thread = json.loads((tmp_path / "slack_threads.json").read_text())["T1"]
    assert [m["text"] for m in thread] == ["перше", "друге"]


def test_grafana_unavailable_is_reported_not_raised(monkeypatch):
    import agents.tools.actions as actions

    def boom(url, payload, headers):
        raise OSError("connection refused")

    monkeypatch.setattr(actions, "_post", boom)
    assert "недоступна" in actions.create_annotation.invoke(
        {"service": "demo-chaos-svc", "text": "розслідування"})["error"]


def test_edit_message_rewrites_in_file_emulation(tmp_path, monkeypatch):
    """Без токена редагування має працювати так само — інакше тести розходяться з проду."""
    import agents.tools.actions as actions

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")
    reference = actions.post_slack.invoke({"thread_id": "T1", "text": "перша редакція"})
    actions.edit_message(reference, "друга редакція")

    thread = json.loads((tmp_path / "slack.json").read_text())["T1"]
    assert len(thread) == 1, "редагування не має додавати повідомлень"
    assert thread[0]["text"] == "друга редакція"


def test_edit_message_uses_chat_update_when_slack_is_on(monkeypatch, tmp_path):
    import agents.tools.actions as actions
    from agents.tools import slack

    calls = []
    monkeypatch.setattr(slack, "update", lambda channel, ts, text: calls.append((channel, ts)))
    actions.edit_message({"transport": "slack", "channel": "C1", "ts": "1700.1"}, "нова")
    assert calls == [("C1", "1700.1")]
