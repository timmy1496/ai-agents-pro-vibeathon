"""Гібридний пошук по KB у Qdrant: dense (e5) + sparse (BM25) зі злиттям RRF.

Гібрид тут не для галочки: у запитах SRE постійно трапляються ідентифікатори
(`payment_ref`, `orders-db`, `OOMKilled`, `HighErrorRate`) — на них dense-вектор
розмивається, а BM25 влучає точно. І навпаки для питань "чому впав чекаут".
Злиття робить сам Qdrant (FusionQuery.RRF), тому свого коду фьюжна тут немає.

Ембеддинги рахує fastembed локально (ONNX) — без API-викликів і без ключів.
"""
from __future__ import annotations

import functools

from qdrant_client import QdrantClient, models

from agents.config import (
    DENSE_MODEL, KB_COLLECTION, KB_DIR, KB_MIN_SCORE, QDRANT_URL, ROOT, SPARSE_MODEL,
)
from agents.kb.chunker import chunk_dir, normalize

DENSE, SPARSE = "dense", "bm25"
PREFETCH_LIMIT = 20  # скільки бере кожен ретривер до злиття
PAYLOAD_FIELDS = ("text", "source", "title", "headings", "type", "service", "services",
                  "date", "tags", "root_cause_label", "severity")


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


def search(query: str, *, limit: int = 5, min_score: float = KB_MIN_SCORE, **equals) -> list[dict]:
    """Гібридний пошук з fail-closed відсіванням: порожній список = 'у базі немає'."""
    query = normalize(query)
    query_filter = _filter(**equals)
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
    return [
        {**point.payload, "score": round(point.score, 4)}
        for point in response.points if point.score >= min_score
    ]


if __name__ == "__main__":
    print(f"indexed {reindex()} chunks into '{KB_COLLECTION}'")
