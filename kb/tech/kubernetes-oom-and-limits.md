---
type: tech
title: OOMKilled, requests і limits
tags: [kubernetes, oom, limits, resources]
---

# OOMKilled, requests і limits

## Як читати
- `OOMKilled` у `k8s_events` — контейнер перевищив `limits.memory`, ядро вбило процес. Це не помилка застосунку, це наслідок.
- `BackOff` після кількох OOM — kubelet сповільнює рестарти.
- Rolling restart без OOM — швидше readiness/liveness, а не пам'ять.

## Форма графіка RSS як діагностика
| Форма | Причина |
|---|---|
| Рівна пилка від деплою | leak у новій версії |
| Плато біля ліміту з першого дня | замалий `limits.memory` |
| Одиночні різкі піки | важка разова операція (великий файл/батч) |

## Правила
- `requests` = p50 реального споживання, `limits` = p99 + 30%.
- Підняття ліміту при підтвердженому leak лише відкладає падіння — це не фікс.
- CPU limit створює throttling; для latency-чутливих сервісів requests важливіші за limits.
