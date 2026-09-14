---
status: "proposed"
date: 2026-09-14
decision-makers: Nikita Bolotov
---

# Ембеддер у кластері: спочатку sidecar, llm-d — коли туди ж переїде генерація

## Context and Problem Statement

На стенді Kubernetes немає навмисно ([ADR-0009](0009-no-kubernetes-on-the-stand.md)),
але наступний крок після демо — кластер. Питання: як там жити ембеддеру з
[ADR-0010](0010-nomic-embed-v2-moe-on-llama-cpp-instead-of-e5-in-process.md) —
sidecar поруч з агентом чи через [llm-d](https://llm-d.ai), стек інференсу на
Kubernetes (Gateway API Inference Extension, Endpoint Picker, vLLM).

Що треба знати про навантаження: KB — 36 чанків, запити — одиниці за хвилину,
модель — 305M активних параметрів, 496 МБ, на CPU відповідає за ~10 мс.
Ембеддинг — один прохід енкодера: немає KV-кешу, немає decode-фази, немає
довгих спільних префіксів між запитами.

## Considered Options

* **Sidecar `llama-server` у поді агента** — нативний sidecar (initContainer з
  `restartPolicy: Always`, k8s ≥ 1.29), агент ходить на `localhost:8081`, CPU-only,
  GGUF тягнеться на старті через `-hf` в `emptyDir` або запікається в образ.
  Плюси: нуль мережевих хопів, життєвий цикл прив'язаний до агента, без GPU, без
  нових CRD, працює в `kind`. Мінуси: копія моделі на кожну репліку агента
  (~1 ГБ RSS на под), холодний старт кожного пода, масштабування зчеплене з агентом.
* **Окремий Deployment + Service** — той самий контейнер, але спільний на всі
  репліки. Це той самий маніфест без прив'язки до пода; має сенс, коли реплік
  агента більше двох або коли ембеддер потрібен ще комусь, крім A1.
* **llm-d: InferencePool + EPP + vLLM у режимі pooling** — vLLM уміє
  ембеддинги (`--runner pooling`, `/v1/embeddings`, для NomicBert потрібен
  `--trust-remote-code`), а llm-d ставить над ним Gateway API Inference Extension
  з Endpoint Picker. Але все, за що llm-d платять — prefix-cache-aware routing,
  KV-cache tiering, prefill/decode-дезагрегація, predicted-latency scheduling —
  для ембеддингів дорівнює нулю: кешувати нічого, декодувати нічого. Лишається
  ціна: gateway-провайдер (kgateway / istio / agentgateway / gke), CRD GAIE,
  helm-чарт роутера, секрет з HF-токеном і практично GPU (CPU-оверлей llm-d
  розрахований на 64 ядра і 64 ГБ на репліку).

## Decision Outcome

Chosen option: «sidecar зараз, llm-d за тригером». Для ембеддера з нашим
навантаженням llm-d — це оркестрація без предмета оркестрації. Sidecar дає те
саме `/v1/embeddings` на localhost за один маніфест, який перевірено на `kind`
(`infra/k8s/embedding/sidecar.yaml`).

llm-d стає доречним не «коли ембеддер виросте», а **коли в кластер переїде
генерація** — сильна модель A2 і дешева для роутера/критика. Тоді ембеддер — ще
один InferencePool за тим самим шлюзом: один вхід, одна автентифікація, одні
метрики і трейси на всі моделі, і саме тоді EPP заробляє свою ціну на LLM-пулах.
Маніфести на цей випадок уже лежать поруч (`infra/k8s/embedding/llmd/`), але
позначені як не прогнані: на цій машині немає ні GPU, ні gateway-провайдера.
Інструкція обома шляхами — [docs/todo/embedding-cluster.md](../todo/embedding-cluster.md).

Тригери переходу з sidecar: (1) LLM у кластері за llm-d; (2) реплік агента
більше двох — спершу на Service, потім за потреби в InferencePool; (3) ембеддер
потрібен іншим командам як сервіс.

### Consequences

* Good, because перший крок у кластер не тягне за собою gateway, CRD і GPU;
  маніфест sidecar піднімається на `kind` за хвилину і читається цілком.
* Good, because контракт агента з ембеддером — URL, і для sidecar, Service, llm-d
  він один і той самий: змінюється лише `KB_EMBED_URL`.
* Bad, because кожна репліка агента гріє власну копію моделі; з `-hf` на старті
  под залежить від доступу до Hugging Face — для прод-образу GGUF треба запікати
  або класти на PVC, інакше рестарт пода = 496 МБ з інтернету.
* Bad, because шлях llm-d описано, але не прогнано: перевірено лише схему
  InferencePool/HTTPRoute проти CRD GAIE (`--dry-run=server`), а не живий трафік
  через шлюз. Перший запуск на GPU-кластері дасть сюрпризи — це очікувано.
