"""HTTP-вхід: авторизація, вебхук приймає алерт швидко, тред накопичує повідомлення."""
import json

import pytest
from fastapi.testclient import TestClient

from agents.config import AGENT_TOKEN

AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agents.app as app_module
    import agents.tools.actions as actions

    slack = tmp_path / "slack.json"
    monkeypatch.setattr(actions, "SLACK_FILE", slack)
    monkeypatch.setattr(app_module, "SLACK_FILE", slack)
    monkeypatch.setattr(app_module, "_handle",
                        lambda text, thread_id: actions.post_slack.invoke(
                            {"thread_id": thread_id, "text": f"звіт по: {text}"}) and "готово")
    return TestClient(app_module.app), slack


def test_webhook_accepts_alert_and_opens_thread(client):
    http, slack = client
    response = http.post("/webhook/alert", headers=AUTH, json={"alerts": [{
        "fingerprint": "abc123",
        "labels": {"alertname": "HighErrorRate", "severity": "critical", "service": "demo-chaos-svc"},
        "annotations": {"summary": "error rate 34%"},
    }]})

    assert response.json() == {"accepted": 1, "threads": ["abc123"]}
    thread = json.loads(slack.read_text())["abc123"]
    assert ":rotating_light:" in thread[0]["text"], "алерт має з'явитись у треді одразу"
    assert "HighErrorRate" in thread[0]["text"] and "demo-chaos-svc" in thread[0]["text"]
    assert any("звіт по" in m["text"] for m in thread), "фонове розслідування мало дописати звіт"


def test_alert_without_fingerprint_still_gets_a_thread(client):
    http, slack = client
    response = http.post("/webhook/alert", headers=AUTH, json={"alerts": [{
        "labels": {"alertname": "FrequentRestarts", "service": "search-api"}, "annotations": {}}]})
    assert response.json()["threads"] == ["FrequentRestarts-search-api"]


def test_approval_is_recorded_in_thread(client):
    http, slack = client
    http.post("/approve", headers=AUTH,
              json={"thread_id": "abc123", "approved": True, "note": "відкочуємо"})
    thread = json.loads(slack.read_text())["abc123"]
    assert any("підтверджено" in m["text"] and "відкочуємо" in m["text"] for m in thread)


def test_approve_without_a_pending_interrupt_is_not_an_error(client):
    """Людина може натиснути «ок» у треді, де нічого не висить — це не 500."""
    http, _ = client
    response = http.post("/approve", headers=AUTH,
                         json={"thread_id": "порожній-тред", "approved": True})
    assert response.status_code == 200
    assert response.json()["resumed"] is False


# --- авторизація -------------------------------------------------------------
#
# /approve — це кнопка HITL. Без токена «підтвердити дію на tier-1» міг будь-хто,
# хто дотягнувся до порту, а uvicorn слухає 0.0.0.0.

@pytest.mark.parametrize("path,payload", [
    ("/webhook/alert", {"alerts": []}),
    ("/sre", {"text": "статус", "thread_id": "t1"}),
    ("/approve", {"thread_id": "t1", "approved": True}),
])
def test_post_endpoints_require_a_token(client, path, payload):
    http, _ = client
    assert http.post(path, json=payload).status_code == 401, f"{path} відкритий"


@pytest.mark.parametrize("header", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": AGENT_TOKEN},          # без схеми
    {"Authorization": "Basic " + AGENT_TOKEN},
])
def test_bad_credentials_are_rejected(client, header):
    http, _ = client
    assert http.post("/approve", headers=header,
                     json={"thread_id": "t1", "approved": True}).status_code == 401


def test_threads_page_renders_and_escapes(client):
    http, slack = client
    slack.write_text(json.dumps({"t1": [{"author": "sre-agent", "text": "<script>x</script>"}]}))
    page = http.get("/").text
    assert "&lt;script&gt;" in page and "<script>x</script>" not in page
