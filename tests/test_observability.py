"""Тули метрик і логів на записаних відповідях — без стенду.

Головне, що тут перевіряється: тул не пропускає сирі дані в промпт.
"""
import json

import pytest

from tests import fixtures


@pytest.fixture
def http(monkeypatch):
    """Підміняє єдиний мережевий виклик модуля; повертає лог викликів."""
    from agents.tools import observability

    calls, responses = [], {}

    def fake_get(url, params):
        calls.append((url, params))
        for marker, payload in responses.items():
            if marker in url:
                return payload
        raise AssertionError(f"немає фікстури для {url}")

    monkeypatch.setattr(observability, "_get", fake_get)
    return type("Http", (), {"calls": calls, "responses": responses})()


def test_prometheus_returns_aggregates_not_points(http):
    from agents.tools.observability import query_prometheus

    http.responses["/api/v1/query_range"] = fixtures.prom_range(
        [0.01, 0.02, 0.34, 0.35], service="demo-chaos-svc")

    result = query_prometheus.invoke({"query": 'rate(http_requests_total[1m])', "minutes": 30})
    series = result["series"][0]
    assert series == {"labels": {"service": "demo-chaos-svc"}, "min": 0.01, "max": 0.35,
                      "avg": 0.18, "last": 0.35, "points": 4}
    assert "values" not in json.dumps(result), "сирі точки ряду не мають потрапляти у вивід"


def test_prometheus_reports_query_error(http):
    from agents.tools.observability import query_prometheus

    http.responses["/api/v1/query_range"] = {"status": "error", "error": "parse error"}
    assert query_prometheus.invoke({"query": "broken{"})["error"] == "parse error"


def test_golden_signals_include_baseline(http):
    from agents.tools.observability import golden_signals

    http.responses["/api/v1/query_range"] = fixtures.prom_range([0.3, 0.35])
    result = golden_signals.invoke({"service": "demo-chaos-svc", "minutes": 30})

    assert set(result["signals"]) == {"error_rate", "latency_p95", "rps", "restarts", "memory_bytes"}
    assert result["signals"]["error_rate"]["baseline_avg"] is not None, \
        "без baseline абсолютне значення нічого не каже"
    assert 'service="demo-chaos-svc"' in result["signals"]["error_rate"]["promql"], \
        "PromQL має бути у виводі — агент цитує його як доказ"
    assert len(http.calls) == 10, "5 сигналів × (вікно + baseline)"


def test_golden_signals_survive_missing_series(http):
    from agents.tools.observability import golden_signals

    http.responses["/api/v1/query_range"] = fixtures.prom_empty()
    signals = golden_signals.invoke({"service": "unknown-svc"})["signals"]
    assert all(s["current_avg"] is None for s in signals.values())


def test_loki_patterns_collapse_noise_and_cap_samples(http):
    from agents.tools.observability import MAX_SAMPLES, query_loki_patterns

    http.responses["/loki/api/v1/query_range"] = fixtures.loki_lines(
        fixtures.ERROR_LOG_LINES * 40)

    result = query_loki_patterns.invoke({"service": "demo-chaos-svc", "top_n": 5})
    patterns = {p["pattern"]: p for p in result["patterns"]}

    assert result["total_lines"] == 200
    assert result["distinct_patterns"] == 3, \
        "два 'request failed' з різними duration_ms мали злитись в один патерн"
    assert any("NullPointer on payment_ref" in p for p in patterns), "патерн релізу втрачено"
    assert any("<ip>" in p for p in patterns), "ip мав нормалізуватись"
    assert all(len(p["samples"]) <= MAX_SAMPLES for p in result["patterns"])
    assert max(p["count"] for p in result["patterns"]) == 80


def test_loki_patterns_use_msg_field_not_whole_json(http):
    from agents.tools.observability import query_loki_patterns

    http.responses["/loki/api/v1/query_range"] = fixtures.loki_lines(fixtures.ERROR_LOG_LINES)
    patterns = [p["pattern"] for p in query_loki_patterns.invoke({"service": "demo-chaos-svc"})["patterns"]]
    assert not any(p.startswith("{") for p in patterns), "патерн має бути з msg, а не з усього JSON"


def test_loki_logs_respects_limit(http):
    from agents.tools.observability import query_loki_logs

    http.responses["/loki/api/v1/query_range"] = fixtures.loki_lines(["boom"] * 50)
    result = query_loki_logs.invoke({"service": "demo-chaos-svc", "contains": "boom", "limit": 5})
    assert len(result["lines"]) == 5


def test_unstructured_log_lines_do_not_crash(http):
    """У Loki потрапляють і не-JSON рядки, і голі числа — тул має їх пережити."""
    from agents.tools.observability import query_loki_patterns

    http.responses["/loki/api/v1/query_range"] = fixtures.loki_lines(
        ["plain text panic", "0.35", "null", '{"msg":"ok"}', "[1,2,3]"])
    result = query_loki_patterns.invoke({"service": "demo-chaos-svc"})
    assert result["total_lines"] == 5 and result["distinct_patterns"] >= 3
