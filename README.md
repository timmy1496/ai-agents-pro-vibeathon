# SRE & DevOps Agent — демо-стенд

Мультиагентна система для SRE/DevOps: реагує на алерти, шукає root cause, відповідає
по базі знань, ревізує сервіси, стежить за метриками після релізу.
Читає вільно, діє тільки через людину (HITL). Усе на синтетиці — жодних прод-доступів.

**Статус:** D1 — стенд, KB, A1 Knowledge, A2 Incident Responder. Далі: A0 Supervisor, датасет і гейт.

```
make install && make test    # 51 тест, ~3 с, без docker і без API-ключа
```

## Швидкий старт

```bash
make up            # підняти стенд (перший раз ~3-5 хв на образи)
make incident-1    # деплой -> 5xx        (root cause: реліз)
make incident-2    # orders-db down       (root cause: залежність)
make incident-3    # memory leak -> OOM   (root cause: ресурси)
make reset         # зняти chaos
make down          # прибрати все
```

Алерт з'являється через 30–90 с: scrape 5s → `for: 30s` → group_wait 10s →
webhook на `http://host.docker.internal:8000/webhook/alert` (агент на хості).

## Що в стенді

| Компонент | Порт | Роль |
|---|---|---|
| Grafana | 3000 | дашборди, annotations, ціль Grafana MCP (admin/admin) |
| Prometheus | 9090 | метрики + alert rules |
| Alertmanager | 9093 | вебхук в агента |
| Loki | 3100 | логи (promtail збирає з docker) |
| Qdrant | 6333 | вектори бази знань |
| Langfuse | 3001 | трейси й вартість (demo@local / demodemo123) |
| chaos-svc | 8080 | демо-сервіс з fault injection |
| podinfo-a/b | — | фонові сервіси каталогу |
| loadgen (k6) | — | 5 rps рівного трафіку на chaos-svc |

## Структура

```
infra/          конфіги prometheus / alertmanager / loki / promtail / grafana
services/       chaos-svc: FastAPI з /chaos/{errors,latency,db-down,oom}
catalog/        services.yaml — 10 сервісів (tier, owner, deps, runbook, SLO)
kb/             postmortems (11) · runbooks (6) · tech (3) — джерело для A1
data/           deploys.json, k8s_events.json — синтетика для get_deploys / k8s_events
scripts/        incident.sh (сценарії), record.py (події), load.js (k6)
```

## Golden signals

`chaos-svc` віддає `http_requests_total`, `http_request_duration_seconds`,
`process_resident_memory_bytes`, `process_start_time_seconds`. Мітку `service`
навішує Prometheus зі scrape target. Рестарти рахуються як
`changes(process_start_time_seconds[10m])` — cAdvisor не потрібен.

## Self-check без стенду

```bash
make selfcheck     # проганяє chaos-режими chaos-svc через TestClient
```

## Агенти

| Агент | Роль | Модель | Тули |
|---|---|---|---|
| A1 Knowledge | відповіді по KB, контекст для інших | дешева | search_kb, similar_incidents, get_service, list_services |
| A2 Incident Responder | RCA за алертом + критик | сильна / дешева на критика | golden_signals, query_loki_patterns, get_deploys, k8s_events, + A1 |

A2 віддає структурований `RCAReport` (`root_cause_label`, `evidence[]` з посиланням на
запит, `recommended_actions`, `confidence`), далі дешевий критик перевіряє groundedness
і за потреби відправляє на доопрацювання — не більше двох обертів.

## Межі агента

- Read-тули читають вільно; write-тулів рівно три: `post_slack`, `create_annotation`,
  `propose_action`. Дії в інфраструктурі агент не виконує ніколи.
- `propose_action` під `HumanInTheLoopMiddleware` — граф зупиняється на interrupt.
- Деструктивні команди (`kubectl delete`, `DROP TABLE`, `rm -rf`, `terraform destroy`…)
  відхиляються до того, як пропозиція дійде до людини.
- `ModelCallLimitMiddleware(run_limit=8)` — стеля кроків на інцидент.
- `PIIMiddleware(apply_to_tool_results=True)` — PII заходить у контекст з лог-рядків,
  а не з питання користувача, тому дефолтного `apply_to_input` тут недостатньо.
- Логи — недовірений текст: у промпт ідуть патерни з лічильником і 3 приклади,
  а не 2000 сирих рядків.

## Свідомі спрощення

- **Kubernetes не піднімаємо.** `k8s_events` читає `data/k8s_events.json`; сигнатура тулу
  така сама, як була б у kind. Додати kind — коли знадобиться реальний scheduling.
- **Langfuse v2, не v3** — один postgres замість clickhouse+redis+minio.
- **Без reranker'а** на 20 документів KB: BM25 + e5-small дають точні хіти по іменах
  сервісів і кодах помилок. Додати bge-reranker — коли KB перевалить за ~200 документів.
- **Slack емулюється** файлом `data/slack_threads.json` — формат повідомлення той самий.
- **RCA-тули б'ють у Prometheus/Loki напряму, не через Grafana MCP.** Бриф вимагає
  Context-Minimization і офлайн-евали на фікстурах — MCP віддає сирі відповіді й тягне
  живий стенд у кожен прогін. Grafana MCP лишається на annotations / alert rules /
  panel images, де він справді унікальний.
