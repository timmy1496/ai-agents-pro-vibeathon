"""A4: порогова логіка детермінована, тому перевіряється без моделі."""
import pytest

from tests import fixtures


@pytest.fixture
def signals(monkeypatch):
    """Задає golden_signals напряму — тут перевіряється порівняння, не збір метрик."""
    from agents.tools import observability

    def make(**values):
        def fake_get(url, params):
            marker = next((n for m, n in observability_markers if m in params.get("query", "")), None)
            current, baseline = values.get(marker, (None, None))
            span = (int(params["end"]) - int(params["start"])) // 60
            value = baseline if span > 15 else current
            return fixtures.prom_range([value] if value is not None else [])

        monkeypatch.setattr(observability, "_get", fake_get)

    from evals.backend import SIGNAL_MARKERS as observability_markers
    return make


def test_healthy_release_passes(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.002, 0.002), latency_p95=(0.25, 0.24), restarts=(0, 0))
    result = compare("checkout-api")
    assert result["status"] == "healthy" and result["breached"] == []


def test_tier1_breach_recommends_rollback(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.20, 0.002), latency_p95=(0.25, 0.24), restarts=(0, 0))
    result = compare("checkout-api")  # tier 1
    assert result["tier"] == 1
    assert result["breached"] == ["error_rate"]
    assert result["status"] == "rollback_recommended", "для tier-1 одного пробиття досить"


def test_tier3_single_breach_is_only_degraded(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.02, 0.002), latency_p95=(0.5, 0.5), restarts=(0, 0))
    result = compare("media-uploader")  # tier 3
    assert result["tier"] == 3 and result["status"] == "degraded"


def test_two_breaches_recommend_rollback_on_any_tier(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.02, 0.002), latency_p95=(3.0, 0.5), restarts=(0, 0))
    assert compare("media-uploader")["status"] == "rollback_recommended"


def test_restarts_use_absolute_count_not_ratio(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.002, 0.002), latency_p95=(0.25, 0.24), restarts=(4, 0))
    result = compare("checkout-api")
    assert "restarts" in result["breached"], "рестарти рахуються штуками, а не відношенням"
    assert "ratio" not in result["deltas"]["restarts"]


def test_zero_baseline_does_not_divide_by_zero(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.05, 0.0), latency_p95=(0.25, 0.24), restarts=(0, 0))
    result = compare("checkout-api")
    assert result["deltas"]["error_rate"]["ratio"] == float("inf")
    assert result["status"] == "rollback_recommended"


def test_missing_metric_does_not_count_as_breach(signals):
    from agents.release_monitor import compare

    signals(error_rate=(None, None), latency_p95=(None, None), restarts=(None, None))
    result = compare("checkout-api")
    assert result["breached"] == [], "відсутня метрика — це не деградація, а брак даних"
    assert result["status"] == "healthy"


def test_unknown_service_falls_back_to_loosest_tier(signals):
    from agents.release_monitor import compare

    signals(error_rate=(0.01, 0.002), latency_p95=(0.25, 0.24), restarts=(0, 0))
    assert compare("no-such-service")["tier"] == 3
