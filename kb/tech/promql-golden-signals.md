---
type: tech
title: PromQL для golden signals
tags: [promql, prometheus, metrics, golden-signals]
---

# PromQL: golden signals стенду

## Error rate
```promql
sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))
  / clamp_min(sum by (service) (rate(http_requests_total[1m])), 0.001)
```
`clamp_min` рятує від ділення на нуль, коли трафіку немає.

## Latency p95
```promql
histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[2m])))
```
Квантиль рахується по гістограмі, тому `sum by (le)` обов'язковий — інакше отримаєш квантиль по одному інстансу.

## Traffic (RPS)
```promql
sum by (service) (rate(http_requests_total[1m]))
```

## Saturation
```promql
process_resident_memory_bytes            # пам'ять
changes(process_start_time_seconds[10m]) # рестарти за 10 хв
```

## Правила читання
- Порівнюй вікно інциденту з baseline тієї ж довжини годину тому — абсолютні числа без baseline не інформативні.
- `rate()` потребує щонайменше двох семплів у вікні: при `scrape_interval: 5s` мінімальне вікно — 15с.
