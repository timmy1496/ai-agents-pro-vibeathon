# ToDo для агента: підняти ембеддер локально (llama.cpp)

Мета: `nomic-embed-text-v2-moe` ([ADR-0010](../adr/0010-nomic-embed-v2-moe-on-llama-cpp-instead-of-e5-in-process.md))
відповідає на `http://127.0.0.1:8081/v1/embeddings`. Кожен крок має перевірку —
не переходь далі, поки вона не зелена. Усі команди прогнано на macOS arm64 (M4 Pro),
llama.cpp 0.4.0 (build 10809).

## 0. Порти

`8080` зайнятий chaos-svc, `8000` — агентом. Ембеддер живе на **8081** — той самий
порт, що в sidecar-маніфесті, тому `KB_EMBED_URL` однаковий локально і в кластері.

## 1. Встановити llama.cpp

- [ ] `brew install llama.cpp`
- [ ] Перевірка: `llama-server --version` → `version: 0.4.0 (build 10809 ...)` або новіше.

Альтернатива без brew — контейнер `ghcr.io/ggml-org/llama.cpp:server` (arm64/amd64,
295 МБ), див. крок 4.

## 2. Запустити сервер

- [ ] `make embed-serve` — те саме, що:

```bash
llama-server -hf nomic-ai/nomic-embed-text-v2-moe-GGUF:Q8_0 \
  --embedding --pooling mean \
  --ctx-size 512 --ubatch-size 512 --batch-size 512 \
  --host 127.0.0.1 --port 8081
```

Перший запуск качає GGUF (496 МБ) у `~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v2-moe-GGUF`,
далі старт займає ~3 с. Прапорці, які не можна губити:

| Прапорець | Навіщо |
|---|---|
| `--embedding` | сервер лише для ембеддингів; без нього `/v1/embeddings` віддає 501 |
| `--pooling mean` | так навчена модель; `cls` або `last` дадуть інший, гірший вектор |
| `--ctx-size 512` | межа моделі (`n_ctx_train = 512`); більше — попередження про overflow |
| `--ubatch-size 512` | один вхід має вміститись у фізичний батч, інакше помилка на довгому чанку |

- [ ] Перевірка: `curl -s localhost:8081/health` → `{"status":"ok"}`.

## 3. Викликати модель

- [ ] `make embed-check` — те саме, що:

```bash
curl -s localhost:8081/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"input": ["search_query: інциденти з OOMKilled", "search_document: # Runbook: OOMKilled"]}'
```

- [ ] Перевірка: у відповіді два вектори по **768** чисел, `usage.prompt_tokens` > 0.

Префікси обов'язкові і на нашому боці: `search_query: ` для запиту,
`search_document: ` для чанка. Без них пошук тихо гіршає, помилки не буде.

## 4. Той самий сервер у контейнері (шлях до compose і sidecar)

- [ ] Кеш моделей монтуємо, щоб не качати вдруге:

```bash
docker run -d --name kb-embedder -p 8081:8080 \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  ghcr.io/ggml-org/llama.cpp:server \
  -hf nomic-ai/nomic-embed-text-v2-moe-GGUF:Q8_0 --embedding --pooling mean \
  --ctx-size 512 --ubatch-size 512 --batch-size 512 --host 0.0.0.0 --port 8080
```

- [ ] Перевірка: `curl -s localhost:8081/health` → `{"status":"ok"}`, модель завантажена за ~3 с (лог `model loaded`).

## 5. Переконатись, що модель підходить саме нашій KB

- [ ] `make embed-bench` — заміряє hit@1/hit@3, діапазони косинуса для релевантних
  і сторонніх запитів та щілину між ними по 36 чанках `kb/`. Очікувано для
  v2-moe: hit@3 13/13, щілина ≈ +0.17. Якщо щілина ≤ 0 — модель не підходить,
  див. таблицю в ADR-0010.
- [ ] Порівняти з поточним e5-large: `make embed-bench EMBED_BENCH_ARGS=--e5`.

## 6. Ollama (альтернатива, не прогнана)

Ollama на цій машині не встановлена; шлях лишено для повноти. `ollama pull
nomic-embed-text` дає **v1.5**, англійську — вона нам не підходить (ADR-0010). Для
v2-moe потрібен Modelfile з локального GGUF:

```bash
curl -L -o nomic-embed-text-v2-moe.Q8_0.gguf \
  https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe-GGUF/resolve/main/nomic-embed-text-v2-moe.Q8_0.gguf
printf 'FROM ./nomic-embed-text-v2-moe.Q8_0.gguf\n' > Modelfile
ollama create nomic-embed-v2-moe -f Modelfile
curl localhost:11434/v1/embeddings -d '{"model":"nomic-embed-v2-moe","input":"search_query: ..."}'
```

## 7. Наступний крок (поза цим ToDo)

Перевести `agents/kb/store.py` з `fastembed` на HTTP до `KB_EMBED_URL` для dense-гілки
(BM25 лишається у fastembed), додати префікси в одному місці, переіндексувати KB,
перекалібрувати `KB_MIN_DENSE_SCORE` (≈0.35), додати `/health` ембеддера в
`make doctor` і сервіс у `docker-compose.yml` з healthcheck. Після цього ADR-0010 →
accepted, ADR-0002 → superseded у частині моделі.
