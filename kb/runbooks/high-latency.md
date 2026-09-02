---
type: runbook
title: Зростання p95 latency
services: [checkout-api, user-profile, demo-chaos-svc]
tags: [latency, p95, saturation, dependency]
---

# Runbook: зростання p95 latency

## Коли застосовувати
Алерт `HighLatencyP95`: p95 > 1s протягом 1 хв (для tier-1 поріг з SLO — 250–400 мс).

## Швидка тріаж-послідовність
1. Розділити latency на власну і залежностей: чи росте p95 на upstream-сервісах з `deps` каталогу.
2. Перевірити насиченість: CPU throttling, RSS біля ліміту, розмір пулу з'єднань до БД.
3. Перевірити логи на таймаути: патерни `i/o timeout`, `context deadline exceeded`, `connection timeout`.
4. Перевірити RPS — чи це не природний сплеск навантаження.

## Найчастіші причини
- **Недоступна або повільна залежність** (БД, кеш, зовнішній API) — latency росте сходинкою, у логах таймаути.
- **Вичерпаний пул з'єднань** — latency росте плавно разом з RPS.
- **CPU throttling** через занизький limit.

## Дії
- Залежність лежить: див. `dependency-down.md`.
- Пул вичерпано: підняти `max_connections`, перевірити відсутність leak з'єднань.
- Throttling: підняти CPU limit, перевірити HPA.

## Перевірка після дії
p95 у межах SLO протягом 15 хв.
