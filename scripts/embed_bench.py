"""Замір ембеддера на реальній KB: чи розділяються релевантні й сторонні запити.

Числа для ADR-0010 отримані саме цим скриптом. Рахує лише dense-гілку (без BM25):
hit@1 / hit@3 за очікуваним джерелом, діапазон максимального косинуса по KB для
релевантних і сторонніх запитів та щілину між ними. Щілина <= 0 означає, що порогу
"у базі немає" для цієї моделі не існує — далі дивитись нема сенсу.

    make embed-bench                       # сервер з KB_EMBED_URL (за замовчуванням :8081)
    make embed-bench EMBED_BENCH_ARGS=--e5 # плюс поточний e5-large через fastembed
    .venv/bin/python -m scripts.embed_bench --url http://127.0.0.1:8092 \
        --doc-prefix "" --query-prefix "Instruct: ...\\nQuery: " --no-layernorm --mrl 512 256

Префікси та layer-norm за замовчуванням — під сімейство nomic (search_query: /
search_document:, LN перед Matryoshka-зрізом, як у reference-коді Nomic).
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

import numpy as np

os.environ.setdefault("QDRANT_URL", ":memory:")  # чанкеру Qdrant не потрібен
from agents.config import KB_DIR, ROOT  # noqa: E402
from agents.kb.chunker import chunk_dir  # noqa: E402

# Релевантні запити з очікуваними джерелами (шляхи відносно kb/). Перші два — кейси
# kb-01 і kb-03 з evals/cases.yaml; пара "OOMKilled" / "політика відпусток" — контрольна
# з ADR-0002, щоб замір можна було звірити зі старими числами.
RELEVANT: list[tuple[str, list[str]]] = [
    ("Чи були у нас інциденти з OOMKilled і що тоді допомогло?",
     ["postmortems/2025-06-02-demo-chaos-svc-memory-leak.md", "postmortems/2025-07-11-search-api-oomkilled.md",
      "postmortems/2025-02-05-media-uploader-oom-large-files.md", "runbooks/oomkilled-restarts.md"]),
    ("Що робити, коли накопичується черга повідомлень?",
     ["runbooks/queue-backlog.md", "postmortems/2025-03-17-notification-worker-queue-backlog.md"]),
    ("orders-db не відповідає, таймаути в demo-chaos-svc",
     ["postmortems/2025-08-30-demo-chaos-svc-orders-db-timeout.md", "runbooks/dependency-down.md"]),
    ("5xx зросли одразу після деплою",
     ["postmortems/2025-10-21-demo-chaos-svc-release-5xx.md", "postmortems/2025-11-04-checkout-api-bad-release.md",
      "runbooks/high-error-rate.md"]),
    ("redis evictions у cart-service",
     ["postmortems/2025-05-19-cart-service-redis-evictions.md", "runbooks/redis-saturation.md"]),
    ("як рахувати golden signals у PromQL", ["tech/promql-golden-signals.md"]),
    ("connection pool вичерпано в user-profile", ["postmortems/2025-04-08-user-profile-connection-pool.md"]),
    ("витік пам'яті у demo-chaos-svc",
     ["postmortems/2025-06-02-demo-chaos-svc-memory-leak.md", "runbooks/oomkilled-restarts.md"]),
    ("LogQL патерни для пошуку помилок у логах", ["tech/logql-patterns.md"]),
    ("memory limits і OOM в Kubernetes", ["tech/kubernetes-oom-and-limits.md", "runbooks/oomkilled-restarts.md"]),
    ("payment-gateway: база даних недоступна",
     ["postmortems/2025-09-15-payment-gateway-db-down.md", "runbooks/dependency-down.md"]),
    ("зміна конфігу podinfo-a зламала сервіс", ["postmortems/2025-01-22-podinfo-a-config-change.md"]),
    ("висока латентність p95, що перевірити", ["runbooks/high-latency.md"]),
]
OFFTOPIC = [
    "Яка у нас політика відпусток для чергових інженерів?",
    "Яка завтра погода в Києві?",
    "Хто виграв чемпіонат світу з футболу?",
    "Як налаштувати принтер в офісі?",
    "Рецепт борщу з квасолею",
    "Скільки коштує підписка на Netflix?",
    "Коли наступна корпоративна вечірка?",
]
COLUMNS = ("model", "dim", "hit1", "hit3", "rel_min", "rel_max", "off_min", "off_max", "gap",
           "oom", "vacation", "t_index_s", "t_query_ms")


def http_embed(url: str, texts: list[str]) -> np.ndarray:
    req = urllib.request.Request(f"{url.rstrip('/')}/v1/embeddings", data=json.dumps({"input": texts}).encode(),
                                 headers={"Content-Type": "application/json"})
    out = json.load(urllib.request.urlopen(req, timeout=600))
    return np.array([d["embedding"] for d in sorted(out["data"], key=lambda d: d["index"])], dtype=np.float32)


def layer_norm(x: np.ndarray) -> np.ndarray:
    return (x - x.mean(-1, keepdims=True)) / np.sqrt(x.var(-1, keepdims=True) + 1e-5)


def l2(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def evaluate(name: str, sources: list[str], docs: np.ndarray, q_rel: np.ndarray, q_off: np.ndarray,
             *, dim: int | None = None, layernorm: bool = False) -> dict:
    if layernorm:
        docs, q_rel, q_off = layer_norm(docs), layer_norm(q_rel), layer_norm(q_off)
    if dim:
        docs, q_rel, q_off = docs[:, :dim], q_rel[:, :dim], q_off[:, :dim]
    docs, q_rel, q_off = l2(docs), l2(q_rel), l2(q_off)
    s_rel, s_off = q_rel @ docs.T, q_off @ docs.T
    hit1 = hit3 = 0
    for i, (_, expected) in enumerate(RELEVANT):
        top = [sources[j] for j in np.argsort(-s_rel[i])[:3]]
        hit1 += top[0] in expected
        hit3 += any(t in expected for t in top)
    rel_max, off_max = s_rel.max(1), s_off.max(1)
    return {
        "model": name, "dim": int(docs.shape[1]), "hit1": f"{hit1}/{len(RELEVANT)}", "hit3": f"{hit3}/{len(RELEVANT)}",
        "rel_min": round(float(rel_max.min()), 3), "rel_max": round(float(rel_max.max()), 3),
        "off_min": round(float(off_max.min()), 3), "off_max": round(float(off_max.max()), 3),
        "gap": round(float(rel_max.min() - off_max.max()), 3),
        "oom": round(float(s_rel[0].max()), 3), "vacation": round(float(s_off[0].max()), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("KB_EMBED_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--name", default=None, help="підпис моделі в таблиці (за замовчуванням — з /v1/models)")
    parser.add_argument("--doc-prefix", default="search_document: ")
    parser.add_argument("--query-prefix", default="search_query: ")
    parser.add_argument("--no-layernorm", action="store_true", help="без LN перед зрізом (не-nomic моделі)")
    parser.add_argument("--mrl", nargs="*", type=int, default=[256], help="Matryoshka-зрізи для порівняння")
    parser.add_argument("--e5", action="store_true", help="додати поточний e5-large через fastembed як baseline")
    parser.add_argument("--json", default=None, help="куди зберегти результати")
    args = parser.parse_args()

    chunks = chunk_dir(KB_DIR, ROOT)
    texts = [c["text"] for c in chunks]
    sources = [c["source"].removeprefix("kb/") for c in chunks]
    rel_queries = [q for q, _ in RELEVANT]
    results: list[dict] = []

    if args.e5:
        from fastembed import TextEmbedding

        model = TextEmbedding("intfloat/multilingual-e5-large")
        t = time.time(); docs = np.array(list(model.embed(texts))); t_index = time.time() - t
        t = time.time(); q_rel = np.array(list(model.query_embed(rel_queries))); t_query = (time.time() - t) / len(RELEVANT)
        q_off = np.array(list(model.query_embed(OFFTOPIC)))
        results.append(evaluate("e5-large (fastembed)", sources, docs, q_rel, q_off)
                       | {"t_index_s": round(t_index, 2), "t_query_ms": round(t_query * 1000, 1)})

    name = args.name or json.load(urllib.request.urlopen(f"{args.url.rstrip('/')}/v1/models"))["data"][0]["id"]
    t = time.time(); docs = http_embed(args.url, [args.doc_prefix + x for x in texts]); t_index = time.time() - t
    t = time.time(); q_rel = http_embed(args.url, [args.query_prefix + q for q in rel_queries]); t_query = (time.time() - t) / len(RELEVANT)
    q_off = http_embed(args.url, [args.query_prefix + q for q in OFFTOPIC])
    ln = not args.no_layernorm
    results.append(evaluate(name, sources, docs, q_rel, q_off, layernorm=ln)
                   | {"t_index_s": round(t_index, 2), "t_query_ms": round(t_query * 1000, 1)})
    results += [evaluate(f"{name}@{d}", sources, docs, q_rel, q_off, dim=d, layernorm=ln) for d in args.mrl]

    print(f"chunks={len(texts)} relevant={len(RELEVANT)} offtopic={len(OFFTOPIC)}  (dense only, cosine)")
    print("\t".join(COLUMNS))
    for r in results:
        print("\t".join(str(r.get(k, "")) for k in COLUMNS))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
