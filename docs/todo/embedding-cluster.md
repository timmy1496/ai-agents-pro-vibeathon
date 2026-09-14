# ToDo для агента: ембеддер у кластері — sidecar і llm-d

Рішення й тригери — в [ADR-0011](../adr/0011-embedding-sidecar-first-llm-d-when-generation-moves-on-cluster.md).
Маніфести — в `infra/k8s/embedding/`. Шлях A перевірено на kind (k8s v1.36.1),
шлях B — лише схемою.

## A. Sidecar (перевірено)

- [ ] Кластер ≥ 1.29 (нативні sidecar-и). Для проби: `kind create cluster --name emb-probe`.
- [ ] `kubectl apply -f infra/k8s/embedding/sidecar.yaml`
- [ ] `kubectl rollout status deploy/sre-agent --timeout=600s` — перший старт тягне
  496 МБ GGUF з Hugging Face (startupProbe дає до 5 хв). На kind вийшло за 87 с.
- [ ] Перевірка 1: `kubectl get pods` → `2/2 Running` (агент + embedder).
- [ ] Перевірка 2: `kubectl logs deploy/sre-agent -c agent` → `embedding dims: 768`
  (контейнер-заглушка агента раз на хвилину кличе `localhost:8081`).
- [ ] Перевірка 3 (спільний Service): з іншого пода
  `curl http://kb-embedder:8081/v1/embeddings -d '{"input":"search_query: черга"}'`
  повертає вектор.
- [ ] Замінити заглушку `python:3.12-slim` на образ агента, коли він з'явиться;
  агент читає `KB_EMBED_URL=http://localhost:8081`.
- [ ] Для проду: зібрати образ із запеченим GGUF
  (`docker build -f infra/k8s/embedding/Dockerfile -t kb-embedder:v2-moe-q8 .`)
  і в маніфесті замінити `image:` + прибрати `-hf`, або покласти GGUF на PVC.
  Інакше кожен рестарт пода йде в інтернет.
- [ ] Прибрати пробу: `kind delete cluster --name emb-probe`.

## B. llm-d (не прогнано — потрібен GPU-кластер і gateway-провайдер)

Робити тільки за тригером з ADR-0011: у кластер переїхала генерація (A2/роутер),
і ембеддер стає ще одним пулом за тим самим шлюзом.

- [ ] Клієнтські інструменти: `kubectl`, `helm`, `helmfile`, `yq`
  (на цій машині всі є, крім `yq`).
- [ ] Клонувати llm-d і підвантажити змінні:

```bash
git clone https://github.com/llm-d/llm-d.git && cd llm-d
export REPO_ROOT=$(pwd) NAMESPACE=llm-d-embed HF_TOKEN=<token>
source guides/env.sh          # GAIE_URL, ROUTER_CHART_VERSION, ROUTER_GATEWAY_CHART
```

- [ ] CRD Gateway API та Inference Extension:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/latest/download/standard-install.yaml
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/${GAIE_URL}/v1-manifests.yaml
```

- [ ] Gateway-провайдер (один з kgateway / istio / agentgateway / gke) — за
  `guides/prereq` llm-d; без нього HTTPRoute ні до чого не прив'язаний.
- [ ] Неймспейс і секрет: `kubectl create ns $NAMESPACE`;
  `kubectl -n $NAMESPACE create secret generic llm-d-hf-token --from-literal=HF_TOKEN=$HF_TOKEN`.
- [ ] Роутер (EPP + gateway) для пулу ембеддера:

```bash
helm install vllm-embed ${ROUTER_GATEWAY_CHART} \
  -f guides/recipes/router/base.values.yaml \
  --set provider.name=<kgateway|istio|agentgateway|gke> \
  --set httpRoute.create=false \
  -n $NAMESPACE --version ${ROUTER_CHART_VERSION}
```

  `httpRoute.create=false`, бо маршрут `/v1/embeddings` наш — `llmd/httproute.yaml`.
  Ім'я EPP-сервісу має збігтись із `endpointPickerRef.name: vllm-embed-epp` у
  `inferencepool.yaml` — звірити після `helm install` (`kubectl get svc -n $NAMESPACE`).
- [ ] Модельний сервер і пул: `kubectl -n $NAMESPACE apply -k infra/k8s/embedding/llmd/`
  (vLLM `--runner pooling --trust-remote-code`, 1 GPU; InferencePool; HTTPRoute).
- [ ] Перевірка 1: `kubectl -n $NAMESPACE get inferencepool vllm-embed` — статус
  Accepted, є endpoints.
- [ ] Перевірка 2: через IP шлюзу
  `curl $GW/v1/embeddings -d '{"model":"nomic-ai/nomic-embed-text-v2-moe","input":"search_query: черга повідомлень"}'`
  → 768 чисел.
- [ ] Перевірка 3: `KB_EMBED_URL=http://$GW` у агента — `make embed-bench` дає ту
  саму щілину, що й локально (модель та сама, вектори мають збігатись до квантизації).

Що вже перевірено без GPU: `kubectl apply --dry-run=server -k infra/k8s/embedding/llmd/`
проходить проти CRD GAIE v1 і Gateway API на kind — схема маніфестів валідна.
