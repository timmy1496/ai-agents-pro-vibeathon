"""Підміна джерел даних записаними виводами кейса.

Завдяки цьому евали бігають без стенду: ні Prometheus, ні Loki, ні Alertmanager
піднімати не треба, а результат відтворюваний від прогону до прогону.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import time

# Який сигнал питає PromQL — визначаємо за маркером у самому запиті.
SIGNAL_MARKERS = (
    ('status=~"5.."', "error_rate"),
    ("histogram_quantile", "latency_p95"),
    ("changes(process_start_time", "restarts"),
    ("process_resident_memory", "memory_bytes"),
    ("http_requests_total", "rps"),
)


def _signal_of(query: str) -> str | None:
    return next((name for marker, name in SIGNAL_MARKERS if marker in query), None)


def _prom_payload(values: list[float]) -> dict:
    now = int(time.time())
    return {"status": "success", "data": {"resultType": "matrix", "result": [{
        "metric": {"service": "fixture"},
        "values": [[now - 30 * (len(values) - i), str(v)] for i, v in enumerate(values)],
    }]}} if values else {"status": "success", "data": {"resultType": "matrix", "result": []}}


def _expand_logs(specs: list[dict]) -> list[str]:
    """Опис логів у кейсі -> рядки. count пишемо числом, щоб не копіювати рядок 40 разів."""
    lines = []
    for spec in specs:
        line = json.dumps({"level": spec.get("level", "error"), "msg": spec["msg"]},
                          ensure_ascii=False)
        lines.extend([line] * spec.get("count", 1))
    return lines


def _loki_payload(lines: list[str]) -> dict:
    now = int(time.time()) * 10**9
    return {"status": "success", "data": {"resultType": "streams", "result": [{
        "stream": {"service": "fixture"},
        "values": [[str(now - i), line] for i, line in enumerate(lines)],
    }]}}


def _stamp(minutes_ago: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def use_fixtures(case, monkeypatch):
    """Вмикає виводи кейса для всіх тулів на час блоку."""  # noqa: D401
    import agents.tools.actions as actions
    import agents.tools.stand as stand
    from agents.tools import observability

    fixtures = case.get("fixtures", {})
    metrics = fixtures.get("metrics", {})

    def fake_get(url: str, params: dict) -> dict:
        if "/loki/" in url:  # шлях Loki містить у собі шлях Prometheus — перевіряти першим
            return _loki_payload(_expand_logs(fixtures.get("logs", [])))
        if "/api/v2/alerts" in url:
            return fixtures.get("alerts", [])
        signal = _signal_of(params.get("query", ""))
        if signal is None:
            return _prom_payload([])
        # golden_signals робить два ЗСУНУТІ вікна: current закінчується «зараз»,
        # baseline — на початку current. Розрізняти за шириною не можна: вона в них
        # може збігтись, а кінець вікна — ні, і саме кінець тут визначальний.
        ends_ago_minutes = (time.time() - int(params["end"])) / 60
        key = "baseline" if ends_ago_minutes > 1 else "current"
        return _prom_payload(metrics.get(signal, {}).get(key, []))

    def fake_recent(filename: str, service: str | None, hours: int) -> list[dict]:
        source = "deploys" if "deploy" in filename else "k8s_events"
        return [
            {**event, "ts": _stamp(event["minutes_ago"]), "service": event.get("service", case["service"])}
            for event in fixtures.get(source, [])
        ]

    monkeypatch.setattr(observability, "_get", fake_get)
    monkeypatch.setattr(stand, "_get", fake_get)
    monkeypatch.setattr(stand, "_recent", fake_recent)
    monkeypatch.setattr(actions, "SLACK_FILE", case["_tmp_slack"])
    monkeypatch.setattr(actions, "_post", lambda url, payload, headers: {"id": 1})
    yield
