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
    monkeypatch.setattr(app_module, "PROCESSED", tmp_path / "processed.json")
    # підміняємо саме розслідування: воно єдине ходить до моделі
    monkeypatch.setattr(app_module, "_investigate_and_post",
                        lambda summary, thread_id: actions.post_slack.invoke(
                            {"thread_id": thread_id, "text": f"звіт по: {summary}"}))
    return TestClient(app_module.app), slack


def test_webhook_accepts_alert_and_opens_thread(client):
    http, slack = client
    response = http.post("/webhook/alert", json={"alerts": [{
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
                        lambda summary, thread_id, alertname="": scheduled.append(thread_id))

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

    first = http.post("/webhook/alert", json={"alerts": [alert()]}).json()
    second = http.post("/webhook/alert", json={"alerts": [alert()]}).json()

    assert first["accepted"] == 1 and first["skipped_duplicates"] == 0
    assert second["accepted"] == 0 and second["skipped_duplicates"] == 1

    thread = json.loads(slack.read_text())[first["threads"][0]]
    assert len(thread) == 2, f"мало бути алерт + звіт, а не {len(thread)} повідомлень"


def test_resolution_adds_one_line_not_a_new_investigation(client):
    http, slack = client

    firing = http.post("/webhook/alert", json={"alerts": [alert()]}).json()
    http.post("/webhook/alert", json={"alerts": [alert(status="resolved")]})

    thread = json.loads(slack.read_text())[firing["threads"][0]]
    assert "вирішено" in thread[-1]["text"]
    assert sum("звіт по" in m["text"] for m in thread) == 1, \
        "закриття інциденту не має запускати розслідування наново"


def test_a_new_firing_after_resolution_is_a_new_incident(client):
    http, slack = client

    first = http.post("/webhook/alert", json={"alerts": [alert()]}).json()
    http.post("/webhook/alert", json={"alerts": [alert(status="resolved")]})
    again = http.post("/webhook/alert",
                      json={"alerts": [alert(started="2026-09-03T14:00:00.000Z")]}).json()

    assert again["accepted"] == 1, "нове загоряння — новий інцидент"
    assert again["threads"][0] != first["threads"][0], "і новий тред"


def test_incident_thread_ends_with_exactly_two_messages(monkeypatch, tmp_path):
    """Алерт і звіт. Вердикт критика дописується у звіт, а не додає третє повідомлення."""
    import agents.app as app_module
    import agents.tools.actions as actions
    from agents.incident_agent import Evidence, RCAReport, Verdict

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")
    monkeypatch.setattr(app_module, "_annotate", lambda service, text: None)

    report = RCAReport(service="demo-chaos-svc", root_cause_label="release",
                       hypothesis="регресія в v1.5.0", confidence=0.93,
                       evidence=[Evidence(fact="error rate 34%", source="PromQL")],
                       recommended_actions=["відкотити"])

    def fake_investigate(alert, config=None, on_report=None, **kwargs):
        on_report(report)  # звіт публікується одразу
        return {"report": report, "verdict": Verdict(grounded=True, verdict="ACCEPT"),
                "revisions": 0, "state": {"messages": []}}

    monkeypatch.setattr("agents.incident_agent.investigate", fake_investigate)
    app_module._process_alert("HighErrorRate critical на demo-chaos-svc: 34%", "t1")

    thread = json.loads((tmp_path / "slack.json").read_text())["t1"]
    assert len(thread) == 2, f"мало бути алерт і звіт, а не {len(thread)}"
    assert thread[0]["text"].startswith(":rotating_light:")
    assert "критик" in thread[1]["text"], "вердикт має опинитись усередині звіту"
    assert "регресія в v1.5.0" in thread[1]["text"]


def test_investigation_is_written_into_thread_memory(monkeypatch, tmp_path):
    """Без цього згадка в треді інциденту приходить у порожній контекст.

    Розслідування йде повз супервізор заради ранньої публікації звіту, тому стан
    треба записати явно — інакше «а чи було таке раніше?» агент не розуміє.
    """
    import agents.app as app_module
    import agents.tools.actions as actions
    from agents.incident_agent import RCAReport, Verdict

    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")
    monkeypatch.setattr(app_module, "_annotate", lambda service, text: None)

    report = RCAReport(service="demo-chaos-svc", root_cause_label="release",
                       hypothesis="регресія в v1.5.0", confidence=0.93,
                       evidence=[], recommended_actions=["відкотити"])
    monkeypatch.setattr("agents.incident_agent.investigate",
                        lambda alert, config=None, on_report=None, **kw: (
                            on_report(report),
                            {"report": report, "verdict": Verdict(grounded=True, verdict="ACCEPT"),
                             "revisions": 0, "state": {"messages": []}})[1])

    app_module._investigate_and_post("HighErrorRate critical на demo-chaos-svc: 34%", "інцидент-1")

    saved = app_module.supervisor.get_state(
        {"configurable": {"thread_id": "інцидент-1"}}).values["messages"]
    assert len(saved) == 2, "у пам'яті треда мають бути питання і звіт"
    assert "регресія в v1.5.0" in saved[-1].content
