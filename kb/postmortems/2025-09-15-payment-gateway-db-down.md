---
type: postmortem
service: payment-gateway
date: 2025-09-15
severity: SEV1
duration_min: 73
root_cause_label: dependency
tags: [database, timeout, 503, dependency, tier1]
---

# PM-2025-09-15 · payment-gateway: недоступна payments-db

## Summary
Failover `payments-db` завис у проміжному стані на 73 хвилини. `payment-gateway` віддавав 503, latency вперлась у клієнтський таймаут 2с.

## Impact
73 хвилини — платежі недоступні. ~$47k незавершених транзакцій (усі відновлені ретраями). SEV1, залучено 6 інженерів.

## Timeline
- 03:12 — планове перемикання primary у `payments-db`.
- 03:14 — `HighLatencyP95` на `payment-gateway`, p95 = 2.0s (таймаут).
- 03:16 — у логах масово `dial tcp payments-db:5432: i/o timeout`.
- 03:21 — виявлено, що новий primary не піднявся, а старий уже read-only.
- 03:40 — увімкнено degraded mode: платежі в чергу замість синхронної обробки.
- 04:25 — БД відновлено вручну, черга розібрана за 12 хвилин.

## Root cause
Скрипт failover не дочекався реплікації і перевів старий primary у read-only до того, як новий прийняв запис. Кластер лишився без writable-вузла.

## Detection
Алерт по latency, не по помилках — сервіс не падав, а чекав на таймаут. Це затримало правильну гіпотезу на 2 хвилини.

## Resolution
Ручне перемикання primary, degraded mode на час відновлення.

## Action items
- [x] Окремий алерт на `dial tcp ... i/o timeout` у логах.
- [x] Circuit breaker перед `payments-db`.
- [ ] Failover-скрипт з перевіркою writable-вузла перед read-only.

## Lessons
Коли latency вперлась рівно в значення таймауту, а RPS не змінився — це майже завжди залежність, а не власний код.
