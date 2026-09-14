# Ембеддер через llm-d (InferencePool + EPP + vLLM pooling)

**Не прогнано.** Схему перевірено лише `kubectl apply --dry-run=server` проти CRD
Gateway API Inference Extension на kind; живого трафіку через шлюз не було — на
машині розробки немає ні GPU, ні gateway-провайдера. Коли цей шлях запускається —
див. [ADR-0011](../../../docs/adr/0011-embedding-sidecar-first-llm-d-when-generation-moves-on-cluster.md).

Порядок (з guides/optimized-baseline llm-d, скорочено під один пул):

1. CRD GAIE: `kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/latest/download/v1-manifests.yaml`
2. Секрет `llm-d-hf-token` з `HF_TOKEN` у неймспейсі.
3. Роутер (EPP + gateway) helm-чартом `oci://ghcr.io/llm-d/charts/llm-d-router-gateway`
   з `provider.name` під ваш gateway (kgateway / istio / agentgateway / gke).
4. `kubectl apply -k .` — Deployment vLLM у режимі pooling, InferencePool, HTTPRoute.
5. Тест: `curl $GATEWAY/v1/embeddings -d '{"model":"nomic-ai/nomic-embed-text-v2-moe","input":"search_query: ..."}'`.
