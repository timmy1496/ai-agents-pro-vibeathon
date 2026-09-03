"""Порожня колекція — найтихіша поломка ретривалу.

Якщо попередній прогін створив колекцію і впав на записі, пошук назавжди повертає
порожньо: тули не падають, тести на них зелені, а агент упевнено каже "у базі немає".
"""


def test_empty_collection_triggers_reindex(monkeypatch):
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    store.reindex()

    # імітуємо обірваний прогін: колекція є, точок немає
    qdrant = store.client()
    qdrant.delete_collection(store.KB_COLLECTION)
    qdrant.create_collection(
        collection_name=store.KB_COLLECTION,
        vectors_config={store.DENSE: __import__("qdrant_client").models.VectorParams(
            size=store._dense_size(),
            distance=__import__("qdrant_client").models.Distance.COSINE)},
    )
    assert qdrant.count(store.KB_COLLECTION).count == 0

    store.ensure_indexed()
    assert qdrant.count(store.KB_COLLECTION).count > 0, \
        "порожня колекція мала переіндексуватись, а не лишитись мовчазною дірою"


def test_existing_populated_collection_is_not_rebuilt(monkeypatch):
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    store.reindex()

    calls = []
    monkeypatch.setattr(store, "reindex", lambda: calls.append(1))
    store.ensure_indexed()
    assert not calls, "непорожню колекцію переіндексовувати не треба"
