import pytest


@pytest.fixture(scope="session")
def kb_indexed():
    """Один індекс KB у пам'яті на всю сесію тестів — переіндексація коштує секунди."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    store.reindex()
    return store
