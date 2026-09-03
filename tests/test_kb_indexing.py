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


def _clients_from_threads(count: int = 3) -> list[int]:
    """Бере клієнт у `count` потоках ОДНОЧАСНО.

    Бар'єр обов'язковий: без нього пул устигає виконати всі задачі одним потоком,
    і тест на потокову ізоляцію нічого не перевіряє.
    """
    import concurrent.futures as futures
    import threading

    from agents.kb import store

    barrier = threading.Barrier(count)

    def take(_):
        barrier.wait(timeout=10)
        return id(store.client())

    with futures.ThreadPoolExecutor(count) as pool:
        return list(pool.map(take, range(count)))


def test_live_server_uses_a_client_per_thread(monkeypatch):
    """Проти живого сервера серіалізувати доступ не можна — під паралельними
    розслідуваннями запити встають у чергу і падають по таймауту."""
    from agents.kb import store

    holder = []

    def fake_client(**kwargs):
        instance = type("FakeClient", (), {})()
        holder.append(instance)  # тримаємо посилання, інакше id() перевикористається
        return instance

    monkeypatch.setattr(store, "QDRANT_URL", "http://example.invalid:6333")
    monkeypatch.setattr(store, "QdrantClient", fake_client)

    assert len(set(_clients_from_threads())) == 3, "кожен потік має отримати власний клієнт"


def test_memory_mode_shares_one_client():
    """У режимі ":memory:" база живе всередині клієнта, тож він мусить бути спільним."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()

    assert len(set(_clients_from_threads())) == 1, "спільна база — спільний клієнт"
