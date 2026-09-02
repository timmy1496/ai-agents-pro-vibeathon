---
type: postmortem
service: demo-chaos-svc
date: 2025-08-30
severity: SEV2
duration_min: 38
root_cause_label: dependency
tags: [database, timeout, 503, latency, orders-db]
---

# PM-2025-08-30 · demo-chaos-svc: таймаути до orders-db

## Summary
`orders-db` перестала приймати з'єднання після вичерпання `max_connections`. `demo-chaos-svc` віддавав 503, p95 виріс до 2с.

## Impact
38 хвилин: замовлення не створювались, читання працювало з кешу. ~2 100 невдалих запитів.

## Timeline
- 16:03 — p95 стрибнув з 120 мс до 2 000 мс.
- 16:04 — `HighLatencyP95` warning.
- 16:06 — у логах `database connection timeout after 2000ms: dial tcp orders-db:5432: i/o timeout`.
- 16:09 — деплоїв за останню годину не було — гіпотезу релізу відкинуто.
- 16:14 — на `orders-db` 100% з'єднань зайнято; винуватець — batch-job аналітики.
- 16:26 — batch-job зупинено, з'єднання звільнились.
- 16:41 — метрики в нормі.

## Root cause
Нічний аналітичний batch-job запустився в денне вікно після зміни cron і зайняв увесь пул з'єднань `orders-db`. Сервіс не мав окремого пулу і чекав на таймаут.

## Detection
Алерт по latency. Ключовий доказ — лог-патерн з таймаутом до конкретного upstream.

## Resolution
Зупинка batch-job, повернення cron-вікна на нічний час.

## Action items
- [x] Окремий connection pool і `statement_timeout` для аналітики.
- [x] Алерт на використання пулу > 80%.
- [ ] Circuit breaker на `orders-db`.

## Lessons
Немає деплою + latency сходинкою + таймаути в логах = дивись на залежність і на те, хто ще її споживає.
