"""A3: чекліст детермінований, тому перевіряється повністю без моделі."""
import pytest
import yaml

from tests import fixtures

GOOD_LOG = ('{"ts":"2026-09-02T10:00:00Z","level":"error","service":"demo-chaos-svc",'
            '"trace_id":"abc123","msg":"order failed"}')
NO_TRACE_LOG = '{"ts":"2026-09-02T10:00:00Z","level":"info","msg":"ok"}'
PII_LOG = '{"level":"info","trace_id":"a1","msg":"user olena@example.com from 10.4.2.11"}'


@pytest.fixture
def backend(monkeypatch):
    from agents.tools import observability, stand

    state = {"logs": [], "rules": []}

    def fake_get(url, params):
        if "/loki/" in url:
            return fixtures.loki_lines(state["logs"])
        if "/api/v1/rules" in url:
            return {"data": {"groups": [{"name": "g", "rules": state["rules"]}]}}
        raise AssertionError(url)

    monkeypatch.setattr(observability, "_get", fake_get)
    monkeypatch.setattr(stand, "_get", fake_get)
    return state


def rule(name, query, severity="critical", duration=30, runbook="kb/runbooks/x.md"):
    return {"type": "alerting", "name": name, "query": query, "duration": duration,
            "labels": {"severity": severity},
            "annotations": {"runbook_url": runbook} if runbook else {}}


# --- логування ---------------------------------------------------------------

def test_reviewer_reads_raw_lines_not_patterns(backend):
    """Патерни втрачають структуру запису — ревізії потрібен оригінал."""
    from agents.service_reviewer import check_logging

    backend["logs"] = [GOOD_LOG] * 10
    assert check_logging("demo-chaos-svc")["metrics"]["structured"] == 1.0


def test_structured_logs_with_trace_score_high(backend):
    from agents.service_reviewer import check_logging

    backend["logs"] = [GOOD_LOG] * 10
    result = check_logging("demo-chaos-svc")
    assert result["grade"] == "A" and result["findings"] == []


def test_missing_trace_id_is_reported(backend):
    from agents.service_reviewer import check_logging

    backend["logs"] = [NO_TRACE_LOG] * 10
    result = check_logging("demo-chaos-svc")
    assert any("trace_id" in f for f in result["findings"])
    assert result["metrics"]["with_trace_id"] == 0.0


def test_pii_in_logs_is_a_finding(backend):
    from agents.service_reviewer import check_logging

    backend["logs"] = [PII_LOG] * 10
    result = check_logging("demo-chaos-svc")
    assert any("PII" in f for f in result["findings"]), "пошта і IP в логах — знахідка"
    assert result["grade"] != "A"


def test_no_logs_at_all_is_grade_f(backend):
    from agents.service_reviewer import check_logging

    backend["logs"] = []
    result = check_logging("demo-chaos-svc")
    assert result["grade"] == "F" and "нічим" in result["findings"][0]


def test_unstructured_logs_fail_structure_check(backend):
    from agents.service_reviewer import check_logging

    backend["logs"] = ["plain text panic at line 42"] * 10
    result = check_logging("demo-chaos-svc")
    assert any("структуровані" in f for f in result["findings"])


# --- алерти -------------------------------------------------------------------

def test_full_golden_signal_coverage_scores_a(backend):
    from agents.service_reviewer import check_alerts

    backend["rules"] = [
        rule("Errors", 'rate(http_requests_total{service="demo-chaos-svc",status=~"5.."}[1m])'),
        rule("Latency", 'histogram_quantile(0.95, http_request_duration_seconds_bucket{service="demo-chaos-svc"})'),
        rule("Restarts", 'changes(process_start_time_seconds{service="demo-chaos-svc"}[10m])'),
        rule("Memory", 'process_resident_memory_bytes{service="demo-chaos-svc"}'),
    ]
    result = check_alerts("demo-chaos-svc")
    assert result["grade"] == "A" and result["missing_signals"] == []


def test_missing_signals_are_named(backend):
    from agents.service_reviewer import check_alerts

    backend["rules"] = [
        rule("Errors", 'rate(http_requests_total{service="demo-chaos-svc",status=~"5.."}[1m])')]
    result = check_alerts("demo-chaos-svc")
    assert set(result["missing_signals"]) == {"latency", "restarts", "saturation"}


def test_alert_without_runbook_lowers_score(backend):
    from agents.service_reviewer import check_alerts

    signals = [
        rule("Errors", 'http_requests_total{service="demo-chaos-svc",status=~"5.."}', runbook=None),
        rule("Latency", 'histogram_quantile(0.95, x{service="demo-chaos-svc"})', runbook=None),
        rule("Restarts", 'process_start_time_seconds{service="demo-chaos-svc"}', runbook=None),
        rule("Memory", 'process_resident_memory_bytes{service="demo-chaos-svc"}', runbook=None),
    ]
    backend["rules"] = signals
    result = check_alerts("demo-chaos-svc")
    assert result["missing_signals"] == [], "покриття повне"
    assert result["grade"] != "A", "алерт без runbook будить людину без інструкції"
    assert any("runbook_url" in f for f in result["findings"])


def test_alert_without_for_is_reported(backend):
    from agents.service_reviewer import check_alerts

    backend["rules"] = [rule("Errors", 'http_requests_total{service="demo-chaos-svc",status=~"5.."}',
                             duration=0)]
    assert any("without for" in f or "без for" in f
               for f in check_alerts("demo-chaos-svc")["findings"])


# --- артефакт -----------------------------------------------------------------

def test_generated_rules_are_valid_yaml_with_tier_settings(backend):
    from agents.service_reviewer import propose_alert_rules

    generated = yaml.safe_load(propose_alert_rules("demo-chaos-svc", ["latency", "restarts"]))
    rules = generated["groups"][0]["rules"]

    assert len(rules) == 2
    for generated_rule in rules:
        assert generated_rule["for"] == "30s", "demo-chaos-svc це tier 1"
        assert generated_rule["labels"]["severity"] == "critical"
        assert generated_rule["annotations"]["runbook_url"].startswith("kb/runbooks/")
        assert 'service="demo-chaos-svc"' in generated_rule["expr"]


def test_tier3_gets_softer_thresholds(backend):
    from agents.service_reviewer import propose_alert_rules

    generated = yaml.safe_load(propose_alert_rules("media-uploader", ["error_rate"]))
    generated_rule = generated["groups"][0]["rules"][0]
    assert generated_rule["for"] == "5m" and generated_rule["labels"]["severity"] == "warning"


def test_latency_threshold_comes_from_slo(backend):
    from agents.service_reviewer import propose_alert_rules

    generated = yaml.safe_load(propose_alert_rules("checkout-api", ["latency"]))
    assert "> 0.25" in generated["groups"][0]["rules"][0]["expr"], "поріг має бути з SLO сервісу"


def test_review_combines_sections_and_attaches_artifact(backend):
    from agents.service_reviewer import review

    backend["logs"] = [GOOD_LOG] * 10
    backend["rules"] = [rule("Errors", 'http_requests_total{service="demo-chaos-svc",status=~"5.."}')]
    result = review("demo-chaos-svc")

    assert {s["section"] for s in result["sections"]} == {"logging", "alerts"}
    assert result["overall_grade"] in "ABCDF"
    assert "groups:" in result["proposed_alert_rules"], "має бути готовий YAML"
    assert yaml.safe_load(result["proposed_alert_rules"])["groups"][0]["rules"]
