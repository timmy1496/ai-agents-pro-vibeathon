"""HTML-звіт: те, що показують суддям, теж має бути під тестом.

Перевіряється не краса, а три речі, у яких звіт може тихо збрехати: n/a показане як
n/a (а не як нуль), вердикт гейта видно не лише кольором, і чужий текст у звіті
екранований.
"""
import pytest

from evals import config, report

DIMENSIONS = list(config.dimensions())


def make_report(gate_passed: bool = True, rows: list[dict] | None = None) -> dict:
    return {
        "meta": {"stamp": "2026-09-03T21:00:00", "rubric_version": config.rubric_version(),
                 "prompts_sha": config.prompts_digest(), "judge_model": "test-judge",
                 "agent_model": "test-agent", "cases": len(rows or [])},
        "summary": {"cases": 1, "root_cause_accuracy": 1.0, "tool_recall": 1.0,
                    "grounded_rate": 1.0, "self_completed": 1.0, "avg_revisions": 0.0,
                    "correctness": 0.9},
        "gate": {"passed": gate_passed,
                 "failures": [] if gate_passed else ["correctness = 0.400 нижче порогу 0.70"]},
        "rows": rows or [],
    }


def row(case_id: str = "rel-01", **overrides) -> dict:
    base = {"case_id": case_id, "root_cause": "release", "root_cause_match": True,
            "missing_tools": [], "revisions": 0, "grounded": True,
            "judge": {"correctness": {"score": 0.9, "rationale": "докази збігаються"}}}
    return {**base, **overrides}


def test_green_and_red_gates_are_distinguishable_without_color():
    """Статусні зелений і червоний нерозрізненні при дейтеранопії (ΔE 4.1).

    Тому вердикт мусить читатись із тексту і гліфа. Якщо колись хтось замінить
    підпис на самий лише кружечок — цей тест впаде.
    """
    green = report.render(make_report(True), None)
    red = report.render(make_report(False), None)

    assert "Гейт зелений" in green and "✓" in green
    assert "Гейт червоний" in red and "✗" in red
    assert "нижче порогу" in red, "причина червоного гейта має бути в самому звіті"


def test_na_dimension_is_shown_as_na_not_zero():
    """n/a намальоване нулем — це найгірше, що може зробити цей звіт.

    Порожня смуга на 0.00 читається як «агент провалив вимір», хоча насправді вимір
    не мав знаменника.
    """
    data = make_report(rows=[row()])
    data["summary"].pop("refusal", None)
    html = report.render(data, None)

    marker = html.split("refusal", 1)[1][:400]
    assert "n/a" in marker, "вимір без знаменника має бути позначений як n/a"
    assert "0.00" not in marker


def test_structural_na_does_not_clutter_the_case_row():
    """Структурний n/a оголошений у toml — це не подія, і в rationale йому нічого робити."""
    judge = {"correctness": {"score": 0.8, "rationale": "ок"},
             "refusal": {"na": "вимір не застосовний", "structural": True},
             "injection": {"na": "судді бракує артефакту", "structural": False}}
    html = report.render(make_report(rows=[row(judge=judge)]), None)

    assert "судді бракує артефакту" in html, "чесний n/a від судді має бути видимим"
    assert "вимір не застосовний" not in html, "структурний n/a не є подією прогону"


def test_untrusted_text_in_a_report_is_escaped():
    """rationale пише модель, а input кейса приходить із лог-рядків. Обидва — не HTML."""
    judge = {"correctness": {"score": 0.5, "rationale": "<script>alert(1)</script>"}}
    html = report.render(make_report(rows=[row(case_id="<img src=x>", judge=judge)]), None)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x&gt;" in html


def test_delta_carries_direction_in_the_glyph_not_only_in_the_color():
    current = make_report(rows=[row()])
    previous = make_report(rows=[row()])
    previous["summary"]["root_cause_accuracy"] = 0.5

    html = report.render(current, previous)
    assert "▲" in html, "напрямок дельти має читатись без кольору"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_every_declared_dimension_gets_a_row(dimension):
    """Вимір, доданий у eval.toml, не має тихо зникнути зі звіту."""
    assert dimension in report.render(make_report(rows=[row()]), None)


def test_report_is_self_contained():
    """Сторінку відкривають з диска і з проєктора — зовнішній запит там просто не піде."""
    html = report.render(make_report(rows=[row()]), None)
    assert "<script" not in html, "звіт статичний: JS йому не потрібен"
    for marker in ("http://", "https://", "cdn"):
        assert marker not in html, f"зовнішнє посилання у звіті: {marker}"
