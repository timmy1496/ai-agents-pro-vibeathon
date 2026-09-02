---
type: postmortem
service: demo-chaos-svc
date: 2025-10-21
severity: SEV2
duration_min: 26
root_cause_label: release
tags: [5xx, release, rollback, orders]
---

# PM-2025-10-21 · demo-chaos-svc: сплеск 5xx одразу після викату

## Summary
Викат v1.3.0 `demo-chaos-svc` дав стрибок 5xx до 35% протягом двох хвилин. Rollback на v1.2.9 усунув проблему.

## Impact
26 хвилин деградації обробки замовлень, ~4 800 помилок 500. Тікетів у підтримку — 12.

## Timeline
- 09:41 — деплой v1.3.0.
- 09:43 — `HighErrorRate` critical, error rate 35%.
- 09:46 — у Loki домінує патерн `unhandled exception in order handler: NullPointer on payment_ref`.
- 09:52 — підтверджено, що інших змін (конфіг, залежності) у вікні не було.
- 09:58 — rollback запущено.
- 10:07 — error rate < 1%, інцидент закрито.

## Root cause
Регресія в обробнику замовлень: новий код не перевіряв наявність `payment_ref` перед використанням. Помилка проявлялась лише на частині трафіку, тому не спливла на stage з синтетичними даними.

## Detection
Алерт `HighErrorRate` (поріг 5%, for 30s). Час до виявлення — 2 хвилини.

## Resolution
Rollback до v1.2.9. Фікс з тестом викотили наступного дня.

## Action items
- [x] Rollback-процедура задокументована в runbook `high-error-rate.md`.
- [ ] Дані stage наблизити до продових за розподілом.

## Lessons
Якщо сплеск 5xx починається в межах 5 хвилин після annotation `deployment` — гіпотеза №1 завжди реліз, а не залежність.
