import pytest


@pytest.fixture(autouse=True)
def no_real_slack(monkeypatch):
    """Жоден тест не має права написати в реальний Slack.

    Транспорт вмикається наявністю SLACK_BOT_TOKEN у .env — тобто варто розробнику
    додати токен, як прогін тестів починає слати повідомлення в робочий канал.
    Тести, яким потрібен Slack-шлях, підміняють _call і вмикають токен явно.
    """
    from agents.tools import slack

    monkeypatch.setattr(slack, "SLACK_BOT_TOKEN", "")


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch, request):
    """Тести не мають права звертатись до реальної моделі.

    DeepSeek (як і будь-який провайдер) — режим роботи, не режим тестування: прогін
    має лишатись безкоштовним, офлайновим і відтворюваним. Тести підставляють
    ScriptedChatModel, а resolve() віддає готовий екземпляр як є, тому це не заважає.

    Виняток — тести самого резолвера: вони перевіряють, що клієнт створюється правильно,
    але не викликають його.
    """
    if request.node.get_closest_marker("uses_real_models") or \
            request.node.module.__name__.endswith("test_models"):
        return

    import agents.models as models

    def refuse(model, temperature=0.0):
        raise AssertionError(
            f"тест намагається створити реальну модель ({model}). "
            f"Підстав ScriptedChatModel з tests/fake_model.py")

    monkeypatch.setattr(models, "_build", refuse)


@pytest.fixture(scope="session")
def kb_indexed():
    """Один індекс KB у пам'яті на всю сесію тестів — переіндексація коштує секунди."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()
    store.reindex()
    return store
