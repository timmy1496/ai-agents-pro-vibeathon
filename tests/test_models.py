"""Резолвер моделі: провайдер визначається ключем, ніде не зашитий жорстко."""
import pytest


@pytest.mark.parametrize("module, attribute", [
    ("agents.knowledge_agent", "build_agent"),
    ("agents.incident_agent", "build_agent"),
    ("agents.incident_agent", "critique"),
    ("agents.incident_agent", "synthesize"),
    ("agents.supervisor", "route"),
    ("agents.release_monitor", "monitor"),
    ("evals.judge", "judge"),
])
def test_every_llm_call_site_goes_through_config(module, attribute):
    """Кожен виклик моделі бере ім'я з config, а не з літерала в коді.

    Інакше зміна CHEAP_MODEL/STRONG_MODEL у .env мовчки не діє на частину системи.
    """
    import importlib
    import inspect

    source = inspect.getsource(getattr(importlib.import_module(module), attribute))
    assert "claude" not in source and "gpt-" not in source and "deepseek" not in source, \
        f"{module}.{attribute} містить зашите ім'я моделі"


def test_openrouter_key_selects_openai_compatible_client(monkeypatch):
    """sk-or-... це OpenRouter: він говорить OpenAI-протоколом, не Anthropic-нативним."""
    import agents.models as models

    monkeypatch.setattr(models, "OPENROUTER_KEY", "sk-or-test")
    models._build.cache_clear()
    client = models.resolve("deepseek/deepseek-v4-flash")

    assert type(client).__name__ == "ChatOpenAI"
    assert "openrouter.ai" in str(client.openai_api_base)
    models._build.cache_clear()


def test_ready_model_instance_passes_through():
    """Тести підставляють готову модель — резолвер не має її чіпати."""
    from agents.models import resolve
    from tests.fake_model import ScriptedChatModel

    scripted = ScriptedChatModel(script=[], calls=[])
    assert resolve(scripted) is scripted
