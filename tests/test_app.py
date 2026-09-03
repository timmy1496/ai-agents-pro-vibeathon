"""HTTP-вхід: вебхук приймає алерт швидко, тред накопичує повідомлення."""
import json

import pytest
from fastapi.testclient import TestClient


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
    response = http.post("/webhook/alert", json={"alerts": [{
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
    response = http.post("/webhook/alert", json={"alerts": [{
        "labels": {"alertname": "FrequentRestarts", "service": "search-api"}, "annotations": {}}]})
    assert response.json()["threads"] == ["FrequentRestarts-search-api"]


def test_approval_is_recorded_in_thread(client):
    http, slack = client
    http.post("/approve", json={"thread_id": "abc123", "approved": True, "note": "відкочуємо"})
    thread = json.loads(slack.read_text())["abc123"]
    assert "підтверджено" in thread[-1]["text"] and "відкочуємо" in thread[-1]["text"]


def test_threads_page_renders_and_escapes(client):
    http, slack = client
    slack.write_text(json.dumps({"t1": [{"author": "sre-agent", "text": "<script>x</script>"}]}))
    page = http.get("/").text
    assert "&lt;script&gt;" in page and "<script>x</script>" not in page


# Тесту на швидкість відповіді вебхука тут навмисно немає: TestClient дочікується
# фонових задач, тому виміряв би себе, а не застосунок. Перевіряється це на живому
# сервері (make agent + curl -w %{time_total}) — див. DEMO.md.
def test_webhook_schedules_work_instead_of_doing_it(monkeypatch, tmp_path):
    """В обробнику не має лишитись ні мережі, ні файлів — лише постановка в чергу."""
    import agents.app as app_module
    import agents.tools.actions as actions

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")
    scheduled = []
    monkeypatch.setattr(app_module, "_process_alert",
                        lambda summary, thread_id: scheduled.append(thread_id))

    with TestClient(app_module.app) as http:
        http.post("/webhook/alert", json={"alerts": [{
            "fingerprint": "f1", "labels": {"alertname": "A", "service": "s"},
            "annotations": {}}]})

    assert scheduled == ["f1"], "уся робота має йти через _process_alert у фоні"


def test_repeated_firing_opens_a_new_thread(client):
    """Той самий алерт, що загорівся вдруге, — це новий інцидент і новий тред.

    Інакше повідомлення лягає у тред з минулого прогону і в каналі його не видно.
    """
    from agents.app import _incident_id

    labels = {"alertname": "HighErrorRate", "service": "demo-chaos-svc"}
    first = _incident_id({"fingerprint": "abc", "startsAt": "2026-09-03T10:00:00.000Z"}, labels)
    same = _incident_id({"fingerprint": "abc", "startsAt": "2026-09-03T10:00:00.000Z"}, labels)
    later = _incident_id({"fingerprint": "abc", "startsAt": "2026-09-03T14:30:00.000Z"}, labels)

    assert first == same, "продовження того самого інциденту — той самий тред"
    assert first != later, "нове загоряння після resolve — новий тред"


def test_incident_id_without_fingerprint_still_works(client):
    from agents.app import _incident_id

    generated = _incident_id({}, {"alertname": "A", "service": "s"})
    assert generated == "A-s", "без fingerprint і startsAt лишається читабельний ключ"
