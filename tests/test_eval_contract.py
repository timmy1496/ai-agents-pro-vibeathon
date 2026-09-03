"""Контракт самого евалу. Вердикт — код виходу pytest, не самозвіт якогось скрипта.

Тут перевіряється не агент, а вимірювальний інструмент. Клас багів, який це закриває:
рубрика тихо поїхала (промпт правили, версію не бампнули), вимір лишився без промпта,
поріг у toml зник, датасет утратив цілу гілку класифікатора. У всіх цих випадках евал
далі бадьоро друкує числа — просто ці числа більше ні з чим не порівняти.

Промпти судді — це і є eval. Тому вони під тим самим гейтом, що й код.
"""
import json

import pytest

from evals import cases, config

DIMENSIONS = config.dimensions()


# --- цілісність рубрики -------------------------------------------------------

def test_rubric_version_matches_its_contents():
    """Правка prompts/*.md без бампу версії не має змерджитись.

    Це головний гард файла. Без нього два різні вимірювальні інструменти їдуть під
    одним ярликом, і завтрашня «регресія агента» може виявитись рухом власної лінійки.
    """
    config.check_rubric_integrity()


def test_rubric_history_never_reuses_a_version_for_a_different_digest():
    """Зворотна помилка: sha перезаписали, версію лишили. prompts_sha її пропускає."""
    history = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
    versions = [k for k in history if not k.startswith("_")]
    digests = [history[v] for v in versions]
    assert len(set(digests)) == len(digests), \
        f"один і той самий digest під різними версіями: {history}"
    assert config.rubric_version() in versions


def test_prompts_sha_actually_notices_a_prompt_edit(tmp_path, monkeypatch):
    """Мутаційний доказ, що гард не декоративний."""
    fake = tmp_path / "prompts"
    fake.mkdir()
    for path in config.PROMPTS_DIR.glob("*.md"):
        (fake / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (fake / "correctness.md").write_text("змінена рубрика", encoding="utf-8")

    monkeypatch.setattr(config, "PROMPTS_DIR", fake)
    config.prompts_digest.cache_clear()
    try:
        with pytest.raises(config.RubricDrift):
            config.check_rubric_integrity()
    finally:
        config.prompts_digest.cache_clear()


# --- виміри -------------------------------------------------------------------

@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_dimension_has_a_prompt(dimension):
    assert config.dimension_prompt(dimension).strip(), f"{dimension}: порожній промпт"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_dimension_tells_the_judge_when_it_does_not_apply(dimension):
    """Без інструкції про n/a суддя ставить 0 там, де немає знаменника.

    Тоді правильна поведінка агента карається: чесна відмова, у якій рекомендованих
    дій і не має бути, опускала б середню actionability так само, як безпорадний звіт.
    """
    assert config.NA_MARKER in config.dimension_prompt(dimension).lower(), \
        f"{dimension}: промпт не каже, коли вимір незастосовний"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_dimension_has_at_least_one_case_to_run_on(dimension):
    """Вимір без жодного застосовного кейса нічого не міряє — і мовчить про це."""
    applicable = [c["id"] for c in cases.load() if config.applicable(dimension, c)]
    assert applicable, f"{dimension}: у датасеті немає кейса, де цей вимір застосовний"


def test_system_prompt_demands_rationale_before_score():
    """Порядок ключів у відповіді судді — частина інструмента, а не оформлення."""
    text = config.system_prompt()
    assert text.index("rationale") < text.index('"score"'), \
        "у схемі відповіді rationale має стояти перед score"


def test_judge_model_is_pinned():
    """Оцінки різних суддів не порівнюються. Пін живе в toml, а не в аргументі за замовчуванням."""
    assert config.load()["judge"]["model"], "модель судді не запінена"


# --- форма датасету -----------------------------------------------------------

def test_dataset_meets_the_declared_shape():
    """Вимоги живуть у eval.toml, тому планка піднімається правкою даних, не коду."""
    rules = config.dataset_rules()
    all_cases = cases.load()
    rca = [c for c in all_cases if c["kind"] == "rca"]

    assert len(all_cases) >= rules["min_cases"], \
        f"у датасеті {len(all_cases)} кейсів при мінімумі {rules['min_cases']}"

    labels = {c["expected_root_cause"] for c in rca}
    missing = set(rules["required_root_causes"]) - labels
    assert not missing, f"жоден кейс не покриває класи причин: {sorted(missing)}"

    refusals = [c for c in all_cases if c.get("must_escalate") or c.get("must_refuse")]
    assert len(refusals) >= rules["min_refusal_cases"], \
        f"кейсів на відмову {len(refusals)}, треба {rules['min_refusal_cases']}"

    injections = [c for c in all_cases if c.get("injection_marker")]
    assert len(injections) >= rules["min_injection_cases"]

    policy = [c for c in all_cases if c["kind"] == "policy"]
    assert len(policy) >= rules["min_policy_cases"]


def test_policy_cases_cover_both_verdicts():
    """Датасет, у якому все блокується, не відрізняє політику від паралічу."""
    verdicts = {c["policy"] for c in cases.by_kind("policy")}
    assert verdicts == {"destructive_blocked", "hitl_required"}, verdicts


def test_gate_thresholds_are_all_declared():
    """Поріг, якого немає в toml, — це поріг, якого немає взагалі."""
    required = {
        "min_root_cause_accuracy", "min_tool_recall", "min_grounded_rate",
        "min_self_completed", "min_correctness", "min_groundedness",
        "min_actionability", "max_drop", "max_unscored",
    }
    assert required <= set(config.gate())
    assert all(0.0 <= value <= 1.0 for value in config.gate().values())


def test_dataset_digest_notices_a_changed_fixture(tmp_path, monkeypatch):
    """Збігу списку id недостатньо: кейс може лишитись собою за іменем і змінитись за суттю.

    cap-02 лишився cap-02, коли його фікстури переписали — стара редакція карала агента
    за правильну відповідь. Дельта через таку правку показала б зміну агента там, де
    змінилось завдання.
    """
    from evals import cases as dataset

    before = config.dataset_digest()
    copy = tmp_path / "cases.yaml"
    copy.write_text(dataset.CASES_FILE.read_text(encoding="utf-8") + "\n# правка\n",
                    encoding="utf-8")

    monkeypatch.setattr(dataset, "CASES_FILE", copy)
    config.dataset_digest.cache_clear()
    try:
        assert config.dataset_digest() != before
    finally:
        config.dataset_digest.cache_clear()


def test_a_run_is_comparable_only_to_its_own_instrument():
    """П'ять полів meta, і кожне з них — частина інструмента, а не налаштування."""
    from evals.run import comparable

    from agents.models import provider

    same = {"rubric_version": config.rubric_version(), "judge_model": config.judge_model(),
            "provider": provider(), "case_ids": [], "dataset_sha": config.dataset_digest()}
    import evals.run as run_module

    run_module._case_ids[:] = []
    assert comparable(same)

    for field, other in (("rubric_version", "r999"), ("judge_model", "інша-модель"),
                         ("provider", "інший-транспорт"), ("case_ids", ["rel-01"]),
                         ("dataset_sha", "0" * 64)):
        assert not comparable({**same, field: other}), \
            f"прогони з різним {field} не мають порівнюватись"
