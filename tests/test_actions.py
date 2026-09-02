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
