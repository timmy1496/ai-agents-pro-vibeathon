"""Порожня колекція — найтихіша поломка ретривалу.

Якщо попередній прогін створив колекцію і впав на записі, пошук назавжди повертає
порожньо: тули не падають, тести на них зелені, а агент упевнено каже "у базі немає".
"""


def test_empty_collection_triggers_reindex(monkeypatch):
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()
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
    store._shared_client.cache_clear()
    store.reindex()

    calls = []
    monkeypatch.setattr(store, "reindex", lambda: calls.append(1))
    store.ensure_indexed()
    assert not calls, "непорожню колекцію переіндексовувати не треба"


def test_embedding_is_shared_and_thread_safe():
    """Одна модель на процес: клієнт-на-потік вантажив по копії e5-large на 2.24 ГБ
    і перетворював count() на дев'ять секунд."""
    import concurrent.futures as futures
    import threading

    from agents.kb import store

    barrier = threading.Barrier(4)

    def embed(text):
        barrier.wait(timeout=30)
        return tuple(store.embed_dense([text])[0][:4])

    with futures.ThreadPoolExecutor(4) as pool:
        vectors = list(pool.map(embed, ["однаковий текст"] * 4))

    assert len(set(vectors)) == 1, "паралельні виклики мають давати той самий вектор"
    assert store._dense_model() is store._dense_model(), "модель має бути одна на процес"


def test_client_holds_no_inference_state():
    """Клієнт став звичайним HTTP-клієнтом — саме тому він потокобезпечний."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()
    assert store.client() is store.client(), "один клієнт на процес"
