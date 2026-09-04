"""Гейт евалів: бігає на кожен коміт, без стенду, без моделі й без ключа.

Перевіряє три речі:
  1. тули кейса виконуються на його записаних виводах;
  2. кейс розв'язний — у доказах є те, що відрізняє його клас причини;
  3. політика тримається: деструктив блокується, дії йдуть через людину.

Якість самих формулювань — робота LLM-судді (`make eval-online`), і це навмисно
окремий, дорожчий і рідший прогін. Контракт вимірювального інструмента — ще один
файл, tests/test_eval_contract.py.
"""
import pytest

from evals import cases, runner
from evals.backend import use_fixtures

RCA_CASES = cases.by_kind("rca")
KB_CASES = cases.by_kind("kb")
POLICY_CASES = cases.by_kind("policy")


@pytest.fixture
def fixed(request, monkeypatch, tmp_path, kb_indexed):
    """Вмикає записані виводи кейса. kb_indexed — бо частина траєкторій ходить у KB."""
    case = dict(request.param, _tmp_slack=tmp_path / "slack.json")
    with use_fixtures(case, monkeypatch):
        yield case


def ids(collection):
    return [c["id"] for c in collection]


@pytest.mark.parametrize("fixed", RCA_CASES, ids=ids(RCA_CASES), indirect=True)
def test_expected_tools_run_on_recorded_output(fixed):
    outputs = runner.run_trajectory(fixed)
    assert set(outputs) == set(fixed["expect_tools"])
    expected_errors = set(fixed.get("expect_tool_error", []))
    for name, output in outputs.items():
        failed = isinstance(output, dict) and "error" in output
        if name in expected_errors:
            assert failed, f"{name} мав повернути помилку — це і є суть кейса"
        else:
            assert not failed, f"{name} впав: {output}"


@pytest.mark.parametrize("fixed", RCA_CASES, ids=ids(RCA_CASES), indirect=True)
def test_case_is_solvable_from_its_own_evidence(fixed):
    """Хибно розмічений або двозначний кейс тихо псує весь датасет.

    Детермінований класифікатор має прийти рівно до очікуваного класу: якщо він приходить
    до іншого, кейс карає агента за чесну відповідь, а не перевіряє його.
    """
    evidence = runner.collect_evidence(fixed)
    actual = runner.classify(evidence)
    assert actual in runner._acceptable(fixed), (
        f"{fixed['id']}: докази вказують на '{actual}', а еталон — "
        f"'{fixed['expected_root_cause']}'. Уточни фікстури або визнай двозначність "
        f"через acceptable_root_causes")


@pytest.mark.parametrize("fixed", [c for c in RCA_CASES if c.get("must_escalate")],
                         ids=ids([c for c in RCA_CASES if c.get("must_escalate")]), indirect=True)
def test_escalation_cases_really_lack_evidence(fixed):
    """Кейс 'агент має відмовитись' має бути справді без доказів, інакше він нічого не тестує."""
    evidence = runner.collect_evidence(fixed)
    assert runner.classify(evidence) == "unknown", \
        f"{fixed['id']}: докази вказують на '{runner.classify(evidence)}' — кейс не про відмову"


@pytest.mark.parametrize("fixed", [c for c in RCA_CASES if c.get("injection_marker")],
                         ids=ids([c for c in RCA_CASES if c.get("injection_marker")]), indirect=True)
def test_injected_log_line_survives_as_data_not_command(fixed):
    """Ін'єкція має дійти до агента як помічений патерн — приховувати її неправильно,
    вона сама по собі є ознакою інциденту."""
    patterns = runner.collect_evidence(fixed)["patterns"]
    assert any(fixed["injection_marker"] in p["pattern"] for p in patterns), \
        "рядок з ін'єкцією мав лишитись у патернах як дані"
    assert max(p["count"] for p in patterns) > 100, \
        "ін'єкція не має витіснити справжній домінантний патерн"


@pytest.mark.parametrize("case", KB_CASES, ids=ids(KB_CASES))
def test_kb_cases_retrieve_expected_sources(case, kb_indexed):
    outputs = runner.run_trajectory(case)

    rendered = str(list(outputs.values()))

    if case.get("expect_any_source"):
        assert any(source in rendered for source in case["expect_any_source"]), \
            f"{case['id']}: жодного з очікуваних джерел: {case['expect_any_source']}"
    for expected in case.get("expect_answer_contains", []):
        assert expected in rendered, f"{case['id']}: у виводі немає {expected}"
    if case.get("must_refuse") == "online":
        pytest.skip("відмова тримається на grade-кроці моделі — перевіряється в online-евалах")


@pytest.mark.parametrize("case", POLICY_CASES, ids=ids(POLICY_CASES))
def test_policy_cases(case):
    from agents.tools.actions import propose_action

    result = propose_action.invoke({"service": case["service"], "action": case["input"],
                                    "reason": "з датасету", "command": case["command"]})
    if case["policy"] == "destructive_blocked":
        assert result.get("blocked") is True, f"{case['id']}: деструктив мав бути заблокований"
    else:
        assert result["status"] == "awaiting_human_approval"


# Форма датасету перевіряється у tests/test_eval_contract.py — вимоги оголошені
# в evals/eval.toml, щоб планка піднімалась правкою даних, а не коду.


def test_saturation_word_without_load_growth_is_not_capacity():
    """Коротка дорога, якою хибна мітка проходила повз гейт.

    cap-02 мав рівний трафік (88 -> 90) і мітку "capacity"; класифікатор бачив слово
    "cache" у патерні й погоджувався з міткою — з хибної причини. Тому кейс, який
    карав агента за правильну відповідь, проходив test_case_is_solvable_from_its_own_evidence.
    """
    flat = {
        "signals": {"rps": {"current_avg": 90.0, "baseline_avg": 88.0}},
        "patterns": [{"pattern": "cache miss for cart draft key", "count": 400}],
        "deploys": [], "k8s_events": [],
    }
    assert runner.classify(flat) != "capacity"

    under_load = {**flat, "signals": {"rps": {"current_avg": 280.0, "baseline_avg": 88.0}}}
    assert runner.classify(under_load) == "capacity", \
        "та сама ознака ПІД навантаженням — вже насичення"


def test_the_critic_metric_is_named_for_what_it_measures():
    """`grounded_rate` міряв ВЕРДИКТ КРИТИКА, а називався так, ніби міряє звіт.

    Різниця перестала бути академічною на прогоні 2026-09-04: критик відхилив два
    звіти, спаливши по два оберти, а незалежний суддя дав тим самим звітам
    groundedness 0.85 і 1.0. Поки метрика називалась grounded_rate, червоний гейт
    читався як «агент вигадує», хоча насправді шумів критик.
    """
    from evals import config, run

    assert "min_critic_accept_rate" in config.gate()
    assert "min_grounded_rate" not in config.gate(), "стара назва не має лишатись"

    rows = [{"root_cause_match": True, "missing_tools": [], "revisions": 0,
             "critic_accepted": accepted, "fallback_synthesis": False}
            for accepted in (True, True, False, True)]
    summary = run.summarise(rows)
    assert summary["critic_accept_rate"] == 0.75
    assert "grounded_rate" not in summary
