---
type: postmortem
service: cart-service
date: 2025-05-19
severity: SEV3
duration_min: 130
root_cause_label: capacity
tags: [redis, cache, evictions, latency, capacity]
---

# PM-2025-05-19 · cart-service: деградація через evictions у Redis

## Summary
`redis-cart` уперся в `maxmemory`, почав витісняти ключі, hit rate впав з 94% до 41%. Latency `cart-service` виросла втричі.

## Impact
130 хвилин підвищеної latency (p95 150 мс → 480 мс). Відмов не було, SLO latency порушено.

## Timeline
- 10:00 — `redis_memory_used_bytes` досягає 98% ліміту.
- 10:12 — evictions починаються, hit rate падає.
- 10:20 — `HighLatencyP95` warning на `cart-service`.
- 10:35 — виявлено префікс `cart:draft:*` без TTL, 3.1 млн ключів.
- 11:10 — TTL 24 год виставлено масово через SCAN + EXPIRE.
- 12:10 — hit rate 91%, latency в SLO.

## Root cause
Півроку тому додали чернетки кошика в Redis без TTL. Обсяг ріс лінійно і врешті витіснив гарячі ключі.

## Detection
Алерт по latency сервісу, а не по стану Redis — сигнал прийшов на 20 хвилин пізніше, ніж міг би.

## Resolution
Масове виставлення TTL на проблемний префікс.

## Action items
- [x] Алерт на `redis_evicted_keys_total > 0`.
- [x] Код-рев'ю правило: кожен SET у кеш має TTL.
- [ ] Окремий інстанс під чернетки.

## Lessons
Кеш деградує тихо: спершу падає hit rate, і лише потім видно latency сервісу. Алертити треба на eviction, а не на наслідок.
