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
    monkeypatch.setattr(app_module, "PROCESSED", tmp_path / "processed.json")
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

    assert response.json()["accepted"] == 1
    assert response.json()["threads"] == ["abc123"]
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


def test_approve_without_a_pending_interrupt_starts_nothing(client, monkeypatch):
    """«Ок» під старим повідомленням не має права запустити нове розслідування.

    Це не гіпотетично: `Command(resume=...)` на треді, де нічого не висить, LangGraph
    виконує як звичайний запуск графа. Поки моделі не було, воно падало і виглядало
    безпечним; щойно модель зʼявилась — тихо витрачало б її і дописувало у тред
    відповідь, якої ніхто не просив.
    """
    import agents.incident_agent as incident

    called = []
    monkeypatch.setattr(incident, "shared_agent",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(
                            AssertionError("граф не мали чіпати")))

    http, _ = client
    response = http.post("/approve", headers=AUTH,
                         json={"thread_id": "порожній-тред", "approved": True})
    assert response.status_code == 200
    assert response.json()["resumed"] is False


def test_approve_resumes_a_thread_that_really_waits(client, monkeypatch):
    """Зворотний бік: коли interrupt справді висить, рішення доходить до графа."""
    import agents.app as app_module
    import agents.incident_agent as incident

    monkeypatch.setattr(incident, "pending_approval", lambda config=None: True)
    monkeypatch.setattr(
        incident, "shared_agent",
        lambda *a, **k: type("G", (), {
            "invoke": staticmethod(lambda *args, **kwargs: {
                "messages": [type("M", (), {"content": "дію зафіксовано"})()]})})())

    http, slack = client
    body = http.post("/approve", headers=AUTH,
                     json={"thread_id": "t9", "approved": True}).json()
    assert body["resumed"] is True
    assert "дію зафіксовано" in json.loads(slack.read_text())["t9"][-1]["text"]


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


def test_failed_investigation_lands_in_the_thread_not_only_in_stderr(monkeypatch, tmp_path):
    """Розслідування бігає у фоні, тому виняток нікому повернути.

    Скінчений ключ або недоступний провайдер не мають виглядати як тред, у якому
    агент просто мовчить: черговий чекає на відповідь, якої вже не буде.
    """
    import agents.app as app_module
    import agents.tools.actions as actions

    slack = tmp_path / "slack.json"
    monkeypatch.setattr(actions, "SLACK_FILE", slack)
    monkeypatch.setattr(app_module, "SLACK_FILE", slack)

    def boom(*args, **kwargs):
        raise RuntimeError("немає ключа моделі")

    monkeypatch.setattr(app_module.supervisor, "invoke", boom)
    http = TestClient(app_module.app)
    http.post("/webhook/alert", headers=AUTH, json={"alerts": [{
        "fingerprint": "f1", "labels": {"alertname": "HighErrorRate", "service": "x"},
        "annotations": {}}]})

    thread = json.loads(slack.read_text())["f1"]
    assert any("не завершилось" in m["text"] and "немає ключа моделі" in m["text"]
               for m in thread), "причина провалу мала опинитись у треді"
    assert any("розбирає людина" in m["text"] for m in thread), \
        "тред має сказати, що алерт лишається на людині"


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
        http.post("/webhook/alert", headers=AUTH, json={"alerts": [{
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


def alert(fingerprint="abc", status="firing", started="2026-09-03T10:00:00.000Z"):
    return {"fingerprint": fingerprint, "status": status, "startsAt": started,
            "labels": {"alertname": "HighErrorRate", "severity": "critical",
                       "service": "demo-chaos-svc"},
            "annotations": {"summary": "error rate 34%"}}


def test_repeated_notification_of_the_same_incident_is_ignored(client):
    """Alertmanager повторює нотифікацію кожен repeat_interval, поки алерт горить.

    Без дедуплікації в тред щохвилини падав би новий звіт RCA.
    """
    http, slack = client

    first = http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert()]}).json()
    second = http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert()]}).json()

    assert first["accepted"] == 1 and first["skipped_duplicates"] == 0
    assert second["accepted"] == 0 and second["skipped_duplicates"] == 1

    thread = json.loads(slack.read_text())[first["threads"][0]]
    assert len(thread) == 2, f"мало бути алерт + звіт, а не {len(thread)} повідомлень"


def test_resolution_adds_one_line_not_a_new_investigation(client):
    http, slack = client

    firing = http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert()]}).json()
    http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert(status="resolved")]})

    thread = json.loads(slack.read_text())[firing["threads"][0]]
    assert "вирішено" in thread[-1]["text"]
    assert sum("звіт по" in m["text"] for m in thread) == 1, \
        "закриття інциденту не має запускати розслідування наново"


def test_a_new_firing_after_resolution_is_a_new_incident(client):
    http, slack = client

    first = http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert()]}).json()
    http.post("/webhook/alert", headers=AUTH, json={"alerts": [alert(status="resolved")]})
    again = http.post("/webhook/alert", headers=AUTH,
                      json={"alerts": [alert(started="2026-09-03T14:00:00.000Z")]}).json()

    assert again["accepted"] == 1, "нове загоряння — новий інцидент"
    assert again["threads"][0] != first["threads"][0], "і новий тред"
