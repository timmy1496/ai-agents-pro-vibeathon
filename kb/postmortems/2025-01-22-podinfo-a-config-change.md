---
type: postmortem
service: podinfo-a
date: 2025-01-22
severity: SEV3
duration_min: 19
root_cause_label: config
tags: [config, 5xx, readiness, platform]
---

# PM-2025-01-22 · podinfo-a: 5xx після зміни readiness-проби

## Summary
Скорочення `readinessProbe.periodSeconds` до 1с з таймаутом 1с призвело до того, що поди періодично випадали з балансування під навантаженням.

## Impact
19 хвилин: ~6% запитів отримали 502 від інгресу.

## Timeline
- 15:40 — застосовано зміну проби.
- 15:44 — `HighErrorRate` warning.
- 15:50 — у подіях видно чергування `Ready`/`NotReady` без рестартів контейнера.
- 15:55 — зміну відкочено.
- 15:59 — помилки зникли.

## Root cause
Проба з таймаутом 1с не переживала GC-паузи і позначала здоровий под як NotReady.

## Detection
Алерт по 5xx на інгресі. Ключ до RCA — чергування Ready/NotReady без рестартів контейнера.

## Resolution
Повернення `periodSeconds: 10`, `timeoutSeconds: 3`.

## Action items
- [x] Мінімальні значення проб зафіксовано в чарті платформи.
- [ ] Лінтер на probe-параметри в CI.

## Lessons
5xx без рестартів і без зростання власної latency сервісу — дивись на інгрес і readiness, а не на код.
