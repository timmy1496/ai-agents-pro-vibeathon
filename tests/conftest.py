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


@pytest.fixture(scope="session")
def kb_indexed():
    """Один індекс KB у пам'яті на всю сесію тестів — переіндексація коштує секунди."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()
    store.reindex()
    return store
