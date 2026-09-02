---
type: runbook
title: Насичення Redis
services: [cart-service]
tags: [redis, cache, saturation, evictions]
---

# Runbook: насичення Redis

## Коли застосовувати
Зростання latency `cart-service` разом з ростом evictions або `maxmemory` біля 100%.

## Швидка тріаж-послідовність
1. `redis_memory_used_bytes / redis_memory_max_bytes`, `redis_evicted_keys_total`.
2. Перевірити hit rate — падіння hit rate множить навантаження на БД.
3. Знайти "гарячі" ключі і TTL, якого немає.

## Найчастіші причини
- Ключі без TTL накопичуються місяцями.
- Зміна формату кешу в релізі — старі й нові ключі співіснують, обсяг подвоюється.

## Дії
- Виставити TTL на проблемний префікс, за потреби тимчасово підняти `maxmemory`.
- Політика `allkeys-lru` замість `noeviction` для чисто кешових інстансів.

## Перевірка після дії
Evictions ≈ 0, hit rate > 90%, latency в SLO.
