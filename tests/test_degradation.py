"""Демо тихої деградації: сервіс змінив формат логів, ніхто нічого не зламав явно.

Це та поломка, яку неможливо помітити очима: тули не падають, агент відповідає,
тести на тули зелені. Червоніє саме гейт евалів — бо докази перестали нести сигнал.
Тест закріплює цю чутливість: якщо він колись стане зеленим, гейт осліп.
"""
import json
import pathlib

import pytest

import evals.backend as backend
from evals import cases, runner
from evals.backend import use_fixtures

DEPENDENCY_CASE = next(c for c in cases.load() if c["id"] == "dep-01")


@pytest.fixture
def renamed_message_field(monkeypatch):
    """Сервіс почав писати `message` замість `msg` — типова зміна після рефакторингу."""
    monkeypatch.setattr(backend, "_expand_logs", lambda specs: [
        json.dumps({"level": spec.get("level", "error"), "message": spec["msg"]},
                   ensure_ascii=False)
        for spec in specs for _ in range(spec.get("count", 1))
    ])


def _evidence(monkeypatch) -> dict:
    case = dict(DEPENDENCY_CASE, _tmp_slack=pathlib.Path("/tmp/degradation-slack.json"))
    with use_fixtures(case, monkeypatch):
        return runner.collect_evidence(case)


def test_baseline_case_is_classified_correctly(monkeypatch):
    assert runner.classify(_evidence(monkeypatch)) == "dependency"


def test_renamed_log_field_makes_the_gate_red(monkeypatch, renamed_message_field):
    """Головна демонстрація: формат змінився -> сигнал зник -> гейт падає."""
    evidence = _evidence(monkeypatch)

    assert runner.classify(evidence) != "dependency", (
        "гейт мав помітити, що докази більше не несуть ознаки залежності")
    assert not runner.is_solvable(DEPENDENCY_CASE, evidence), \
        "саме ця перевірка червоніє в CI і показує деградацію"


def test_degradation_is_invisible_to_the_tools_themselves(monkeypatch, renamed_message_field):
    """Чому це «тихо»: жоден тул не падає і не скаржиться."""
    evidence = _evidence(monkeypatch)

    assert evidence["patterns"], "тул логів відпрацював без помилок"
    assert evidence["signals"]["error_rate"]["current_avg"] is not None, "метрики теж цілі"
    top_pattern = evidence["patterns"][0]["pattern"]
    assert "timeout" not in top_pattern.lower(), \
        "текст повідомлення перетворився на структурний шум — ось де втрачається сигнал"
