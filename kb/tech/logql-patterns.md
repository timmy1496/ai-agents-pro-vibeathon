---
type: tech
title: LogQL і пошук патернів помилок
tags: [logql, loki, logs, patterns]
---

# LogQL: пошук патернів у логах

## Помилки сервісу за вікно
```logql
{service="demo-chaos-svc"} | json | level="error"
```

## Топ-патерни замість сирих рядків
```logql
sum by (pattern) (count_over_time({service="demo-chaos-svc"} | pattern `<_>` [5m]))
```
Для агента це головний запит: сирі лог-рядки — це untrusted text і дорогий контекст. У промпт має йти агрегований патерн з лічильником, а не 500 рядків.

## Швидкість помилок
```logql
sum(rate({service="demo-chaos-svc"} | json | level="error" [1m]))
```

## Типові патерни стенду
- `unhandled exception in order handler: NullPointer on payment_ref` → регресія релізу.
- `database connection timeout ... dial tcp orders-db:5432: i/o timeout` → недоступна залежність.
- `memory ballast grew` → витік пам'яті.
