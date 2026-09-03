"""Суддя: розбір контракту відповіді і межа між «немає знаменника» і «не виміряли».

Модель тут підставна — перевіряється сам вимірювальний інструмент, а не якість оцінок.
"""
import pytest

from evals import config, judge as judging


class FakeJudge:
    """Суддя зі сценарієм відповідей."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def invoke(self, messages):
        self.seen.append(messages[-1]["content"])
        return type("M", (), {"content": self.answers.pop(0)})()


@pytest.fixture
def graded(monkeypatch):
    """Підміняє резолвер моделі; повертає фабрику, що ставить сценарій."""
    holder = {}

    def install(*answers):
        holder["judge"] = FakeJudge(*answers)
        monkeypatch.setattr("agents.models.resolve", lambda *a, **k: holder["judge"])
        return holder["judge"]

    return install


CASE = {"id": "rel-01", "kind": "rca", "service": "demo-chaos-svc",
        "input": "HighErrorRate", "expected_root_cause": "release"}


def score(name="correctness", case=None):
    return judging.score_dimension(name, case or CASE, None, "лог")


# --- контракт відповіді -------------------------------------------------------

def test_valid_answer_is_parsed(graded):
    graded('{"rationale": "докази збігаються", "score": 0.8}')
    result = score()
    assert isinstance(result, judging.Score)
    assert result.value == 0.8


def test_rationale_must_come_before_score(graded):
    """Порядок ключів — частина інструмента: оцінка, виведена з міркування, і оцінка,
    обґрунтована після нього, це різні речі. Тому порядок перевіряється, а не мається на увазі."""
    graded('{"score": 0.8, "rationale": "потім придумав"}',
           '{"score": 0.8, "rationale": "і вдруге те саме"}')
    assert isinstance(score(), judging.Unscored)


def test_honest_na_is_not_a_zero(graded):
    graded('{"rationale": "дій тут і не має бути", "score": {"na": "кейс на відмову"}}')
    result = score()
    assert isinstance(result, judging.NotApplicable)
    assert result.structural is False, "це n/a від судді, а не оголошений у toml"


def test_out_of_range_score_is_refused(graded):
    graded('{"rationale": "r", "score": 1.7}', '{"rationale": "r", "score": 1.7}')
    assert isinstance(score(), judging.Unscored)


# --- ретрай -------------------------------------------------------------------

def test_prose_answer_gets_one_reminder_and_recovers(graded):
    """Суддя час від часу пише прозою з заголовками. Без ретраю це коштувало кейса."""
    fake = graded("## Оцінка\\n\\nДії добрі, десь 0.8.",
                  '{"rationale": "з другої спроби за контрактом", "score": 0.8}')
    result = score()

    assert isinstance(result, judging.Score) and result.value == 0.8
    assert "[СИСТЕМА]" in fake.seen[1], "друга спроба має нести нагадування про контракт"
    assert "[СИСТЕМА]" not in fake.seen[0], "перша спроба — чистий артефакт"


def test_two_bad_answers_become_unscored_not_zero(graded):
    """Не виміряли — це не нуль. Нуль занизив би оцінку агента за поломку інструмента."""
    graded("проза раз", "проза два")
    result = score()

    assert isinstance(result, judging.Unscored)
    assert "error" in result.as_dict() and "score" not in result.as_dict()


# --- ізоляція збоїв -----------------------------------------------------------

def test_one_broken_dimension_does_not_take_the_others(graded, monkeypatch):
    """Агент свою роботу вже зробив; збій вимірювання не має забирати решту оцінок."""
    def flaky(name, case, report, tool_log, model=None):
        if name == "actionability":
            raise RuntimeError("обірвався виклик")
        return judging.Score(0.9, "ок")

    monkeypatch.setattr(judging, "score_dimension", flaky)
    scores = judging.judge(CASE, None, "лог")

    assert scores["correctness"]["score"] == 0.9
    assert "error" in scores["actionability"]
    assert "score" not in scores["actionability"]


def test_unscored_dimensions_are_counted_but_not_averaged():
    """Знаменник середнього — лише виміряне. Невиміряне рахується окремо."""
    rows = [
        {"judge": {"correctness": {"score": 0.8, "rationale": "r"}}},
        {"judge": {"correctness": {"error": "суддя мовчить"}}},
        {"judge": {"correctness": {"na": "не застосовно", "structural": True}}},
    ]
    assert judging.average(rows, "correctness") == 0.8, "n/a і помилки не в знаменнику"
    assert judging.unscored(rows) == 1


def test_structural_na_never_spends_a_judge_call(graded):
    """Незастосовність оголошена в toml — питати суддю нема про що."""
    fake = graded()  # порожній сценарій: будь-який виклик впаде
    result = judging.score_dimension("refusal", CASE, None, "лог")

    assert isinstance(result, judging.NotApplicable) and result.structural
    assert not fake.seen, "структурний n/a не має коштувати жодного виклику"


def test_every_declared_dimension_is_attempted(graded):
    graded(*['{"rationale": "r", "score": 0.7}'] * len(config.dimensions()))
    scores = judging.judge(CASE, None, "лог")
    assert set(scores) == set(config.dimensions())
