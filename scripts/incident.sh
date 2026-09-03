#!/usr/bin/env bash
# Запускає один з трьох демо-сценаріїв стенду.
# Алерт має спрацювати через 30-90 с (scrape 5s + for 30s + group_wait 10s).
set -euo pipefail

CHAOS="${CHAOS_URL:-http://localhost:8080}"
GRAFANA="${GRAFANA_URL:-http://admin:admin@localhost:3000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

annotate() {
  curl -sf -X POST "$GRAFANA/api/annotations" \
    -H 'Content-Type: application/json' \
    -d "{\"text\":\"$1\",\"tags\":[\"deployment\",\"demo-chaos-svc\"]}" >/dev/null \
    && echo "grafana annotation: $1" || echo "warn: grafana annotation не створено"
}

case "${1:-}" in
  1)
    echo "== Сценарій 1: деплой нової версії -> зростання 5xx =="
    python3 "$ROOT/scripts/record.py" deploy demo-chaos-svc 1.5.0
    python3 "$ROOT/scripts/record.py" k8s demo-chaos-svc Created "Created container chaos-svc (v1.5.0)"
    annotate "deploy demo-chaos-svc v1.5.0"
    curl -sf -X POST "$CHAOS/chaos/errors?rate=0.35" && echo
    echo "-> очікуй алерт HighErrorRate за ~1 хв"
    ;;
  2)
    echo "== Сценарій 2: недоступна залежність orders-db -> latency + timeouts =="
    curl -sf -X POST "$CHAOS/chaos/db-down?enabled=true" && echo
    python3 "$ROOT/scripts/record.py" k8s orders-db Unhealthy "Readiness probe failed: dial tcp 10.4.2.11:5432: i/o timeout"
    echo "-> очікуй алерт HighLatencyP95 за ~1.5 хв"
    ;;
  3)
    echo "== Сценарій 3: memory leak -> OOMKilled рестарти =="
    curl -sf -X POST "$CHAOS/chaos/oom?mb_per_sec=16" && echo
    python3 "$ROOT/scripts/record.py" k8s demo-chaos-svc OOMKilling "Memory cgroup out of memory: Killed process (uvicorn), limit 256Mi"
    python3 "$ROOT/scripts/record.py" k8s demo-chaos-svc BackOff "Back-off restarting failed container chaos-svc"
    echo "-> очікуй HighMemoryUsage за ~1 хв, далі FrequentRestarts"
    ;;
  reset)
    echo "== Скидання chaos =="
    curl -sf -X POST "$CHAOS/chaos/reset" && echo
    # маркер витоку прибирає сам /chaos/reset, тому рестарт уже чистий
    docker compose restart chaos-svc >/dev/null && echo "chaos-svc перезапущено"
    ;;
  *)
    echo "usage: $0 <1|2|3|reset>" >&2
    exit 1
    ;;
esac
