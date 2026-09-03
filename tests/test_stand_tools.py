"""Тули деплоїв, подій кластера і активних алертів."""
import json

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import agents.tools.stand as stand

    monkeypatch.setattr(stand, "DATA_DIR", tmp_path)
    return tmp_path


def write(path, events):
    path.write_text(json.dumps(events))


def test_get_deploys_filters_by_service_and_window(data_dir):
    import datetime as dt

    from agents.tools.stand import get_deploys

    now = dt.datetime.now(dt.UTC)
    stamp = lambda hours: (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write(data_dir / "deploys.json", [
        {"ts": stamp(1), "service": "demo-chaos-svc", "version": "1.5.0"},
        {"ts": stamp(3), "service": "demo-chaos-svc", "version": "1.4.2"},
        {"ts": stamp(99), "service": "demo-chaos-svc", "version": "1.0.0"},
        {"ts": stamp(1), "service": "checkout-api", "version": "2.0.0"},
    ])

    deploys = get_deploys.invoke({"service": "demo-chaos-svc", "hours": 6})
    assert [d["version"] for d in deploys] == ["1.5.0", "1.4.2"], "має бути свіже першим"


def test_missing_data_file_returns_empty_not_error(data_dir):
    from agents.tools.stand import k8s_events

    assert k8s_events.invoke({"service": "demo-chaos-svc"}) == []


def test_k8s_events_surface_oom(data_dir):
    import datetime as dt

    from agents.tools.stand import k8s_events

    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write(data_dir / "k8s_events.json", [
        {"ts": stamp, "service": "demo-chaos-svc", "type": "Warning",
         "reason": "OOMKilling", "message": "Memory cgroup out of memory"},
    ])
    events = k8s_events.invoke({"service": "demo-chaos-svc"})
    assert events[0]["reason"] == "OOMKilling" and events[0]["type"] == "Warning"


def test_active_alerts_are_flattened(monkeypatch):
    import agents.tools.stand as stand

    monkeypatch.setattr(stand, "_get", lambda url, params: [{
        "labels": {"alertname": "HighErrorRate", "service": "demo-chaos-svc", "severity": "critical"},
        "annotations": {"summary": "error rate 34%", "runbook_url": "kb/runbooks/high-error-rate.md"},
        "startsAt": "2026-09-02T10:00:00Z",
    }])

    alerts = stand.get_active_alerts.invoke({"service": "demo-chaos-svc"})
    assert alerts[0]["alertname"] == "HighErrorRate"
    assert alerts[0]["runbook_url"] == "kb/runbooks/high-error-rate.md"


def test_alertmanager_down_is_reported_not_raised(monkeypatch):
    """Підміняємо _safe_get: саме він перетворює збій джерела на читабельну відповідь."""
    import agents.tools.stand as stand

    monkeypatch.setattr(stand, "_safe_get", lambda url, params: None)
    assert "недоступний" in stand.get_active_alerts.invoke({})[0]["error"]
