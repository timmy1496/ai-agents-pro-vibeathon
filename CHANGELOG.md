# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/uk/1.1.0/). Рішення з обґрунтуванням
живуть в ADR (`docs/adr/`), тут — що змінилось і де це подивитись.

## [Unreleased]

### Added
- **ADR-0010** — модель ембеддингів для KB: `nomic-embed-text-v2-moe` (Q8_0, GGUF) на
  `llama-server` окремим процесом замість `multilingual-e5-large` усередині процесу через
  fastembed. Вибір заміряний на 36 чанках KB: запропонований `nomic-embed-text-v1.5`
  англійський і на українській KB дає від'ємну щілину між релевантними й сторонніми
  запитами (−0.014, як MiniLM в ADR-0002); v2-moe дає +0.175 проти +0.042 у e5,
  496 МБ замість 2.24 ГБ, індексація KB 1.7 с замість 9.2 с. Qwen3-Embedding-0.6B —
  запасний варіант (+0.146, контекст 32k). Статус proposed до перемикання `store.py`.
  ([docs/adr/0010](docs/adr/0010-nomic-embed-v2-moe-on-llama-cpp-instead-of-e5-in-process.md))
- **ADR-0011** — ембеддер у кластері: спочатку нативний sidecar `llama-server` у поді
  агента (перевірено на kind, k8s 1.36); llm-d (InferencePool + EPP + vLLM pooling) — за
  тригером, коли в кластер переїде генерація, бо для ембеддингів prefix-cache/KV-routing
  не дають нічого. ([docs/adr/0011](docs/adr/0011-embedding-sidecar-first-llm-d-when-generation-moves-on-cluster.md))
- **ToDo для агента** з перевірками на кожному кроці:
  [docs/todo/embedding-local.md](docs/todo/embedding-local.md) (brew / контейнер / Ollama
  як не прогнана альтернатива) і [docs/todo/embedding-cluster.md](docs/todo/embedding-cluster.md)
  (sidecar і llm-d).
- `infra/k8s/embedding/sidecar.yaml` — Deployment з sidecar-ембеддером і Service
  `kb-embedder`; `Dockerfile` з запеченим GGUF для проду; `llmd/` — Deployment vLLM у
  режимі pooling, InferencePool, HTTPRoute (схема пройшла `--dry-run=server` проти CRD
  GAIE v1; живий трафік не прогнано — немає GPU і gateway-провайдера).
- `make embed-serve` / `make embed-check` / `make embed-bench` і `scripts/embed_bench.py` —
  запуск ембеддера на `:8081`, перевірка виклику і замір hit@k та щілини на KB, яким
  отримані числа ADR-0010.

### Changed
- README: таблиця документації посилається на 11 ADR, ToDo й цей changelog.

### Not done (свідомо)
- `agents/kb/store.py` досі рахує dense-вектори через fastembed/e5: перемикання на
  `KB_EMBED_URL`, переіндексація і перекалібрування `KB_MIN_DENSE_SCORE` — окремий крок
  (див. ToDo, розділ 7). Ollama-шлях описано, але не прогнано: Ollama не встановлена.
