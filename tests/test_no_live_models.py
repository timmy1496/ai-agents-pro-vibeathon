"""Запобіжник: прогін тестів не має витрачати гроші й ходити в мережу за моделлю."""
import pytest


def test_building_a_real_model_is_blocked_in_tests():
    from agents.models import resolve

    with pytest.raises(AssertionError, match="реальну модель"):
        resolve("deepseek/deepseek-v4-flash")


def test_scripted_model_still_passes_through():
    from agents.models import resolve
    from tests.fake_model import ScriptedChatModel

    scripted = ScriptedChatModel(script=[], calls=[])
    assert resolve(scripted) is scripted, "підставна модель має проходити без змін"
