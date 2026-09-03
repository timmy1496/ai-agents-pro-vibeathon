"""A3: чекліст детермінований, тому перевіряється повністю без моделі."""
import pathlib

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


# --- ревізія проти ПРАВИЛ САМОГО СТЕНДУ ---------------------------------------
#
# Решта тестів годує правила, у яких ім'я сервісу стоїть усередині expr. Стенд так
# правил не пише: golden-signals описані один раз і агрегують `by (service)`. Через це
# світ тестів не збігався зі світом стенду — і рівно на демо A3 ставив F здоровому
# сервісу, бо фільтр шукав ім'я сервісу в тексті виразу. Ці два тести читають той самий
# файл, який монтується в Prometheus, тому розбіжність більше не може бути тихою.

STAND_RULES = pathlib.Path(__file__).resolve().parent.parent / "infra/prometheus/rules/alerts.yml"


def stand_rules() -> list[dict]:
    """Правила стенду у формі, в якій їх віддає /api/v1/rules."""
    groups = yaml.safe_load(STAND_RULES.read_text())["groups"]
    return [
        {"type": "alerting", "name": r["alert"], "query": r["expr"],
         "duration": _seconds(r.get("for", "0s")), "labels": r.get("labels", {}),
         "annotations": r.get("annotations", {})}
        for g in groups for r in g["rules"]
    ]


def _seconds(duration: str) -> int:
    return int(duration.rstrip("smh")) * {"s": 1, "m": 60, "h": 3600}[duration[-1]]


def test_service_agnostic_stand_rules_cover_the_service(backend):
    """Правило без селектора сервісу покриває всі сервіси — зокрема й цей."""
    from agents.service_reviewer import check_alerts

    backend["rules"] = stand_rules()
    result = check_alerts("demo-chaos-svc")

    assert result["missing_signals"] == [], (
        f"здоровий сервіс на правилах стенду лишився без покриття: {result['findings']}")
    assert result["grade"] in ("A", "B"), result


def test_rule_pinned_to_another_service_does_not_count(backend):
    """Зворотний бік: правило з явним селектором чужого сервісу не покриває наш."""
    from agents.service_reviewer import check_alerts

    backend["rules"] = [rule("OtherErrors",
                             'rate(http_requests_total{service="payment-gateway",status=~"5.."}[1m]) > 0.05')]
    assert "error_rate" in check_alerts("demo-chaos-svc")["missing_signals"]


def test_regex_selector_matches_the_service(backend):
    from agents.service_reviewer import check_alerts

    backend["rules"] = [rule("Errors",
                             'rate(http_requests_total{service=~"demo-.*",status=~"5.."}[1m]) > 0.05')]
    assert "error_rate" not in check_alerts("demo-chaos-svc")["missing_signals"]


def test_alert_without_for_lowers_the_score(backend):
    """Раніше without_for потрапляв у findings, але на бал не впливав."""
    from agents.service_reviewer import check_alerts

    full = [rule(f"R{i}", q) for i, q in enumerate(
        ['http_requests_total{status=~"5.."}', "histogram_quantile(0.95, x)",
         "process_start_time_seconds", "process_resident_memory_bytes"])]
    backend["rules"] = full
    with_for = check_alerts("demo-chaos-svc")["score"]

    backend["rules"] = [{**r, "duration": 0} for r in full]
    without_for = check_alerts("demo-chaos-svc")["score"]

    assert without_for < with_for, "алерт без for будить людину на одиничному викиді"


def test_doctor_notices_prometheus_serving_stale_rules():
    """Демо-пастка: змонтований файл змінили, Prometheus не перечитав.

    Правки в alerts.yml не доїжджають у Prometheus без /-/reload, і він далі мовчки
    віддає старі правила. Це вже стріляло: після мерджу ревізія A3 бачила чотири
    правила зі старими `for`, виглядала правдоподібно — а стенд жив за іншими.
    """
    from scripts.doctor import _seconds, _stale_rules

    actual = {r["alert"]: _seconds(str(r.get("for", "0s"))) for r in stand_rules_raw()}
    assert not _stale_rules(actual), "файл сам із собою розходитись не може"

    drifted = {name: value + 60 for name, value in actual.items()}
    assert set(_stale_rules(drifted)) == set(actual), "розбіжність по `for` має бути помічена"


def stand_rules_raw() -> list[dict]:
    return [r for g in yaml.safe_load(STAND_RULES.read_text())["groups"] for r in g["rules"]]
