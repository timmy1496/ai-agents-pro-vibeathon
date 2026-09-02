---
type: postmortem
service: checkout-api
date: 2025-11-04
severity: SEV2
duration_min: 42
root_cause_label: release
tags: [5xx, release, rollback, tier1]
---

# PM-2025-11-04 · checkout-api: 5xx після релізу v2.11.0

## Summary
Через 3 хвилини після викату v2.11.0 частка 5xx на `checkout-api` зросла з 0.1% до 34%. Відкат на v2.10.4 повернув сервіс до норми за 8 хвилин.

## Impact
42 хвилини часткової недоступності оформлення замовлення. ~11 400 невдалих запитів, 380 покинутих кошиків. SLO доступності за місяць спалено на 61%.

## Timeline
- 14:02 — деплой v2.11.0 (annotation `deployment` у Grafana).
- 14:05 — алерт `HighErrorRate`, error rate 34%.
- 14:07 — on-call у треді, перша гіпотеза "БД" відкинута: latency БД у нормі.
- 14:12 — у логах домінує патерн `nil pointer dereference in payment_ref mapper`.
- 14:19 — рішення на rollback (tier-1, правило "10 хвилин").
- 14:27 — v2.10.4 у проді, error rate < 1%.
- 14:44 — інцидент закрито.

## Root cause
У v2.11.0 змінили контракт відповіді `payment-gateway`: поле `payment_ref` стало опційним. Клієнтський маппер у `checkout-api` розіменовував його без перевірки на nil. Для замовлень без збереженої карти поле було відсутнє — це ~1/3 трафіку.

## Detection
Алерт `HighErrorRate` спрацював за 3 хвилини. Кореляція з деплоєм була очевидна з Grafana annotation — саме вона скоротила тріаж.

## Resolution
Rollback до попередньої версії. Виправлення поїхало окремо через 2 дні з тестом на відсутнє поле.

## Action items
- [x] nil-guard у мапері + unit-тест на відсутній `payment_ref`.
- [x] Контрактні тести між `payment-gateway` і `checkout-api` у CI.
- [ ] Canary-деплой на 5% трафіку для tier-1.

## Lessons
Grafana annotation про деплой — найдешевший сигнал для RCA: перше питання при сплеску 5xx завжди "що змінилось за останню годину".
