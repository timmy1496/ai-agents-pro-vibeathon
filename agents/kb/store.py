"""Гібридний пошук по KB у Qdrant: dense (e5) + sparse (BM25) зі злиттям RRF.

Гібрид тут не для галочки: у запитах SRE постійно трапляються ідентифікатори
(`payment_ref`, `orders-db`, `OOMKilled`, `HighErrorRate`) — на них dense-вектор
розмивається, а BM25 влучає точно. І навпаки для питань "чому впав чекаут".
Злиття робить сам Qdrant (FusionQuery.RRF), тому свого коду фьюжна тут немає.

Ембеддинги рахує fastembed локально (ONNX) — без API-викликів і без ключів.
"""
from __future__ import annotations

import functools
import threading

from qdrant_client import QdrantClient, models

from agents.config import (
    DENSE_MODEL, KB_COLLECTION, KB_DIR, KB_MIN_DENSE_SCORE, QDRANT_URL, ROOT, SPARSE_MODEL,
)
from agents.kb.chunker import chunk_dir, normalize

DENSE, SPARSE = "dense", "bm25"
PREFETCH_LIMIT = 20  # скільки бере кожен ретривер до злиття
PAYLOAD_FIELDS = ("text", "source", "title", "headings", "type", "service", "services",
                  "date", "tags", "root_cause_label", "severity")


# ponytail: один RLock на весь доступ до Qdrant. Причина не в самій базі, а в клієнті:
# при локальному інференсі QdrantClient тримає спільний акумулятор батчів ембеддингів і
# ламається, коли агент кличе тули паралельно ("dictionary changed size during iteration",
# а на індексації — напівстворені вектори). Ціна: пошук по KB серіалізований.
# Прибрати, коли ембеддинги рахуватиме окремий сервіс, а не клієнт у процесі агента.
_lock = threading.RLock()


def _guarded(function):
    """Серіалізує виклик — див. коментар біля _lock."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)

    return wrapper


@functools.cache
def client() -> QdrantClient:
    # ":memory:" — локальний режим без docker: так евали й тести бігають офлайн.
    return QdrantClient(location=QDRANT_URL) if QDRANT_URL.startswith(":") \
        else QdrantClient(url=QDRANT_URL)


@functools.cache
def _dense_size() -> int:
    from fastembed import TextEmbedding

    return next(m["dim"] for m in TextEmbedding.list_supported_models()
                if m["model"] == DENSE_MODEL)


def _documents(text: str) -> dict:
    return {DENSE: models.Document(text=text, model=DENSE_MODEL),
            SPARSE: models.Document(text=text, model=SPARSE_MODEL)}


@_guarded
def reindex() -> int:
    """Перебудовує колекцію з нуля. KB маленька — інкрементальність тут зайва."""
    qdrant = client()
    chunks = chunk_dir(KB_DIR, ROOT)

    if qdrant.collection_exists(KB_COLLECTION):
        qdrant.delete_collection(KB_COLLECTION)
    qdrant.create_collection(
        collection_name=KB_COLLECTION,
        vectors_config={DENSE: models.VectorParams(size=_dense_size(),
                                                   distance=models.Distance.COSINE)},
        # modifier=IDF обов'язковий для BM25: без нього Qdrant не рахує зворотну частоту
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    qdrant.upsert(
        collection_name=KB_COLLECTION,
        points=[
            models.PointStruct(id=i, vector=_documents(c["text"]),
                               payload={k: c[k] for k in PAYLOAD_FIELDS})
            for i, c in enumerate(chunks)
        ],
    )
    if not QDRANT_URL.startswith(":"):  # у локальному режимі payload-індекси не працюють
        for field in ("service", "type", "root_cause_label"):
            qdrant.create_payload_index(KB_COLLECTION, field, models.PayloadSchemaType.KEYWORD)
    return len(chunks)


def _filter(**equals) -> models.Filter | None:
    """Фільтр по точних значеннях payload; None-и ігноруються."""
    conditions = [
        models.FieldCondition(key=key, match=models.MatchValue(value=value))
        for key, value in equals.items() if value
    ]
    return models.Filter(must=conditions) if conditions else None


def ensure_indexed() -> None:
    """Індексує KB, якщо колекції ще немає.

    Потрібно і для режиму ":memory:" (кожен процес починає з порожньої бази), і для
    першого запуску проти живого Qdrant.

    Перевірка всередині лока — щоб другий потік не переіндексував услід за першим.

    Умова — саме непорожність, а не існування колекції. Якщо попередній прогін створив
    колекцію і впав на записі, порожня колекція лишається назавжди, а пошук мовчки
    повертає "у базі немає": тули не падають, тести зелені, агент упевнено відповідає,
    що інформації немає. Це найгірший різновид поломки, і коштує він одного count().
    """
    with _lock:
        qdrant = client()
        if not qdrant.collection_exists(KB_COLLECTION) or qdrant.count(KB_COLLECTION).count == 0:
            reindex()


@_guarded
def _best_dense_score(query: str, query_filter: models.Filter | None) -> float:
    """Максимальний косинус по dense-гілці — єдиний сигнал з абсолютною шкалою."""
    response = client().query_points(
        collection_name=KB_COLLECTION,
        query=models.Document(text=query, model=DENSE_MODEL),
        using=DENSE, limit=1, query_filter=query_filter, with_payload=False,
    )
    return response.points[0].score if response.points else 0.0


@_guarded
def search(query: str, *, limit: int = 5, min_score: float = KB_MIN_DENSE_SCORE, **equals) -> list[dict]:
    """Гібридний пошук з fail-closed відсіванням: порожній список = 'у базі немає'.

    Ранжує RRF (він точніший), а відсікає dense-косинус (у нього є шкала). Це два
    різні питання: "що релевантніше" і "чи є тут узагалі відповідь".
    """
    ensure_indexed()
    query = normalize(query)
    query_filter = _filter(**equals)
    if _best_dense_score(query, query_filter) < min_score:
        return []
    response = client().query_points(
        collection_name=KB_COLLECTION,
        prefetch=[
            models.Prefetch(query=models.Document(text=query, model=DENSE_MODEL),
                            using=DENSE, limit=PREFETCH_LIMIT, filter=query_filter),
            models.Prefetch(query=models.Document(text=query, model=SPARSE_MODEL),
                            using=SPARSE, limit=PREFETCH_LIMIT, filter=query_filter),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )
    return [{**point.payload, "score": round(point.score, 4)} for point in response.points]


if __name__ == "__main__":
    print(f"indexed {reindex()} chunks into '{KB_COLLECTION}'")
