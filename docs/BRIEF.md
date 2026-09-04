# SRE & DevOps Agent — бриф для вайбатону

> **Джерело.** Версія 0.1 від 2026-09-01, автор Nikita Bolotov. Опублікований у Slack
> `#learning-ai-agents-pro-vibetone`, ідея — [Google Doc](https://docs.google.com/document/d/1pBNWYDeZD3UYA3yxc7WioVcDjG1Fmt8OQQPgOBUUajI/edit).
> Курс: AI Agents.PRO (fwdays academy).
>
> Копія в репо навмисно **дослівна і незмінна**: це те, з чим звіряється зроблене.
> Відхилення від брифу не правляться тут — вони називаються і обґрунтовуються в
> [README](../README.md) і в [ADR](adr/). Що з брифу реалізовано, а що ні — таблиця в
> кінці цього файла.

## 1. Що будуємо

Мультиагентна система для SRE/DevOps, яка (а) реагує на алерти і по запиту шукає root
cause, (б) відповідає на питання по базі знань компанії (постмортеми, RCA, runbooks,
сервіс-каталог), (в) робить ревізію сервісу і пропонує алерти/дашборди, (г) стежить за
метриками після релізу. Усе — на демо-стенді з синтетичними даними. Читає вільно,
**діє тільки через людину** (HITL): жодних змін в інфраструктурі без підтвердження.

Цільова «фішка» для суддів (два SRE + CTO): агент не просто «чатить про логи», а
проходить **траєкторію інциденту** з перевіркою кожного кроку — і це видно у трейсах
Langfuse і в eval-гейті.

## 2. Обмеження й рамки

| Що | Як |
|---|---|
| Команда / час | 1–2 людини, 1–2 дні → один наскрізний сценарій має працювати ідеально, решта — тонко |
| Середовище | Демо-стенд (docker-compose), синтетика. Жодних прод-доступів |
| Стек | LangChain 1.x `create_agent` · LangGraph (supervisor, checkpointer) · LangSmith (evals) · Langfuse (observability, self-host у compose) |
| Інструменти | **Grafana MCP** (`mcp-grafana`: Prometheus, Loki, alert rules, dashboards, annotations) — закриває 80% tool-шару · Slack (вхід/вихід) · GitHub (PR з правилами алертів) · власний MCP `catalog` (сервіс-каталог + KB) |
| Моделі | Каскад: дешева (Haiku 4.5 / Flash) для router, grade, judge; сильна (Sonnet 5) для RCA-синтезу |
| Безпека | Rule of Two: агент читає чуже (логи, алерти) + бачить приватне (метрики, каталог) → вихід назовні тільки в Slack-тред і PR, обидва allowlisted; write-tools за HITL; PreToolUse-хук блокує деструктивне |

## 3. Розбиття на агентів

Принцип з курсу: починаємо з одного циклу, ділимо там, де є чіткі межі відповідальності
і різні tool-allowlist'и. Кожен агент — `create_agent` з власним system prompt, набором
tools і моделлю; зверху — LangGraph supervisor. Спільні для всіх: `search_kb`, `get_service`.

### A0 · Supervisor / Router

* **Роль:** єдина точка входу (Slack mention, Alertmanager webhook, `/sre` команда).
  Класифікує інтент → маршрутизує на A1–A4. Тримає стан діалогу (checkpointer,
  `thread_id` = Slack-тред) і довгострокову пам'ять (Store: сервіс → останні інциденти).
* **Інтенти:** `ALERT` · `RCA` · `KB` · `REVIEW` · `RELEASE` · `HUMAN`.
* **Патерн:** Router → воркер → (для ALERT/RCA) Evaluator-критик з лімітом 2 оберти.
* **Модель:** дешева. **Tools:** тільки маршрутизація + `ask_human`.

### A1 · Knowledge Agent (шар знань) — фундамент

* **Роль:** відповідає на питання по базі знань і віддає контекст іншим агентам. Це
  subagent: інші питають його вузьким промптом, назовні — лише підсумок.
* **Дані (синтетика):** `catalog/services.yaml` — 6–10 сервісів (name, tier, language,
  owner, deps, dashboards, runbook, SLO); `kb/postmortems/*.md` — 10–15 постмортемів у
  єдиному шаблоні; `kb/runbooks/*.md` — 5–8 runbooks; `kb/tech/*.md` — технології.
* **Retrieval:** chunk 300±100 токенів по H2/H3, метадані `service, type, date, tags`;
  hybrid (BM25 + локальні e5-small) + reranker `bge-reranker-v2-m3` — бо в запитах будуть
  ідентифікатори сервісів і коди помилок; поріг fail-closed → «у базі немає».
* **Tools:** `search_kb`, `get_service`, `list_services`, `similar_incidents`.
* **Сховище:** Qdrant у compose. **Модель:** дешева.

### A2 · Incident Responder — головний сценарій демо

**Тригер:** Alertmanager/Grafana webhook → Slack-тред; або запит RCA у треді.

**Траєкторія** (агент вирішує сам, але датасет очікує саме ці інструменти):

1. `get_alert` → сервіс, severity, labels → `get_service` (tier, owner, deps, runbook).
2. `query_prometheus` — golden signals ±30 хв (error rate, p95, saturation, restarts).
3. `query_loki` — error-патерни за вікно, top-N.
4. `k8s_events` / `get_deploys` — що змінилось: деплой, рестарти, OOM, config change.
5. `similar_incidents` → A1: чи було схоже.
6. Синтез: гіпотеза root cause + докази (з посиланнями на запити) + рекомендовані дії з
   runbook + confidence.
7. Evaluator (дешева модель): чи кожен факт підкріплений tool-виводом? REVISE → максимум
   2 оберти.
8. Пост у Slack-тред; `create_annotation` у Grafana; пропозиція дій → кнопка
   «підтвердити» (HITL).

* **Tools (read-only):** Grafana MCP (`query_prometheus`, `query_loki_logs`,
  `query_loki_patterns`, `get_alert_group`, `get_annotations`), `k8s_events`,
  `get_deploys`, `similar_incidents`. **Write:** `post_slack`, `create_annotation`,
  `propose_action` (HITL).
* **Модель:** сильна для синтезу; дешева для критика.
* **Guardrails:** логи — untrusted text → Context-Minimization перед синтезом (витяг
  патернів, не сирі рядки); allowlist доменів у відповіді; ліміт 8 кроків циклу.

### A3 · Service Reviewer (ревізія + пропозиція алертів/дашбордів)

**Тригер:** `/sre review <service>`.

**Чекліст** (детермінований, у коді, не в промпті):

* *Логування:* структуровані JSON? рівні? `trace_id`? PII/секрети в логах? частка ERROR
  без контексту?
* *Ресурси:* requests/limits задані? HPA? OOMKilled за 7 днів? CPU throttling?
* *Алерти:* покриття golden signals відносно tier; runbook_url у labels? noisy alerts?
* *Дашборди:* є per-service dashboard? панелі відповідають SLO?
* **Вихід:** скоркарт (A–F по розділу) + артефакти: PromQL alert rules (YAML) з `for`,
  severity по tier, runbook-лінком; dashboard JSON. Доставка — PR у git-репо стенду або
  `update_dashboard` за HITL.
* **Патерн:** Orchestrator–Worker: fan-out на 4 підперевірки (`Send`), агрегатор зводить
  у скоркарт.
* **MVP-обріз:** логування + алерти → скоркарт + YAML правил. Дашборди — roadmap.

### A4 · Release Monitor (метрики після релізу)

* **Тригер:** deploy-подія (annotation/webhook) → таймер 15 хв; або `/sre release <service>`.
* **Логіка (майже без LLM):** baseline = 1 год до деплою, window = 15 хв після; дельти
  error rate, p95, RPS, restarts, memory з tier-залежними порогами. LLM лише для вердикту
  в Slack: healthy / degraded / rollback recommended, з графіком (`get_panel_image`).
* **Tools:** `get_deploys`, `query_prometheus`, `get_panel_image`, `post_slack`.
  **Write (HITL):** `propose_rollback`.
* Це workflow, не агент — детермінований вузол LangGraph з одним LLM-кроком.

### Roadmap (не в MVP)

* **A5 · Infra Upgrade Planner** — план оновлення по каталогу: інвентаризація версій,
  breaking changes з KB, порядок за tier/deps. Кандидат на A2A-інтеграцію з Pippin для
  масових PR.
* Dashboard builder як окремий агент; онлайн-евали на трафіку; A2A Agent Card.

## 4. Демо-стенд

`docker-compose`: Prometheus + Alertmanager + Loki + Promtail + Grafana (provisioning
датасорсів і 2 дашбордів) + Qdrant + Langfuse + 3 демо-сервіси (`podinfo` ×2 + один свій
з fault-injection: `/chaos/latency`, `/chaos/errors`, `/chaos/oom`, `/chaos/db-down`).
Kubernetes — опційно `kind`. Скрипт `make incident-1` вмикає chaos → через 1–2 хв
спрацьовує алерт → webhook в агента.

**3 сценарії для демо і датасету:**

1. Деплой нової версії → зростання 5xx (root cause: реліз, є схожий постмортем).
2. БД-залежність недоступна → latency + timeouts у логах (root cause: dependency).
3. Memory leak → OOMKilled рестарти (root cause: ресурси).

## 5. Observability + Evaluation

* **Langfuse** (`@observe`, OTel): трейс = дерево supervisor → agent → tool call; токени
  і вартість на інцидент. KPI: вартість на успішно розібраний інцидент.
* **Датасет** 20–25 кейсів: вхід (алерт/запит + записані tool-виводи), очікувані tools,
  очікуваний root-cause label, еталонний звіт. 2–3 кейси «агент має відмовитись / ескалювати».
* **Дві перевірки на кейс:** (1) детермінована — чи викликано очікувані tools і не
  викликано write-tools без HITL (pytest, кожен коміт, $0); (2) LLM-judge — correctness /
  groundedness / actionability (на реліз).
* **Гейт у CI:** `pytest test_eval_gate.py` з порогом; звіт дельти до попереднього прогону.
  Демо «тихої деградації»: змінили формат логів → гейт червоніє.
* Записані tool-виводи (fixtures) → евали бігають офлайн без стенду.

## 6. Безпека — чек-лист

* Tool-allowlist per agent; write-tools за `HumanInTheLoopMiddleware`.
* PreToolUse-хук: deny `alerting_manage_rules` з `delete`, будь-який `kubectl delete`,
  зміни поза `alerts/` у PR.
* `PIIMiddleware` на логи до потрапляння в LLM.
* Логи/алерти — untrusted: тест «промпт-ін'єкція в лог-рядку» — у датасет.
* Окрема identity агента (Grafana service account read-only + окремий для write), egress
  тільки Slack/GitHub.
* `ModelCallLimitMiddleware(run_limit=8)`, `SummarizationMiddleware` при 80% вікна.

## 7. План на 1–2 дні

| Крок | Що | Результат |
|---|---|---|
| D1 ранок | Стенд: compose + chaos + alert rules + Grafana MCP; синтетичний каталог і 10 постмортемів | `make up`, `make incident-1` → алерт |
| D1 день | A1 Knowledge + A2 Incident Responder з Langfuse | Сценарій 1 end-to-end у Slack-треді |
| D1 вечір | A0 Supervisor + checkpointer; датасет 10 кейсів + детермінований гейт | `pytest` зелений |
| D2 ранок | A4 Release Monitor + A3 Reviewer (логування + алерти → YAML + PR) | Сценарії 2–3 |
| D2 день | Датасет до 20, LLM-judge, дельта, деградаційне демо; guardrails-тести | Гейт + звіт дельти |
| D2 вечір | README, схема, 5-хв демо-скрипт, запис | Здача |

Якщо часу бракує — ріжемо в такому порядку: дашборди → PR → A3 повністю → A4 повністю.
**A1 + A2 + евали + Langfuse не ріжемо** — це кістяк оцінки.

## 8. Відкриті питання

1. Slack-інтеграція: реальний workspace (тестовий канал) чи емуляція?
2. Kubernetes на стенді (`kind`) — робимо чи імітуємо events файлом?
3. LangSmith для judge/датасету чи все в Langfuse (v4 має datasets + LLM-as-judge)?
4. Мова відповідей агента: українська (судді) чи англійська?

---

## Що з брифу зроблено, а що ні

Станом на 2026-09-04. Це не оцінка брифу — це звірка.

| Пункт брифу | Стан | Де пояснено |
|---|---|---|
| A0 Supervisor + checkpointer | ✅ усі шість інтентів мають воркера | — |
| A1 Knowledge, гібридний пошук | ✅ Qdrant RRF: e5-large + BM25 | [ADR-0002](adr/0002-e5-large-instead-of-minilm-for-kb-retrieval.md) |
| A2 Incident Responder + критик | ✅ повна траєкторія, критик з лімітом 2 | — |
| A3 Service Reviewer | ✅ MVP-обріз: логування + алерти → скоркарт + YAML | [ADR-0007](adr/0007-a3-and-a4-are-deterministic-not-llm.md) |
| A4 Release Monitor | ✅ tier-залежні пороги, один LLM-крок | [ADR-0007](adr/0007-a3-and-a4-are-deterministic-not-llm.md) |
| HITL на write-тулах | ✅ + PreToolUse-guard до людини | [ADR-0003](adr/0003-destructive-guard-as-middleware-not-inside-the-tool.md) |
| PIIMiddleware на логи | ✅ `apply_to_tool_results=True` | — |
| Context-Minimization | ✅ патерни з лічильником замість сирих рядків | [ADR-0001](adr/0001-direct-prometheus-loki-tools-instead-of-grafana-mcp.md) |
| Тест на ін'єкцію в лозі | ✅ кейс `inj-01`, вимір `injection` | [EVALS.md](EVALS.md) |
| Датасет 20–25 кейсів | ✅ 24 | [EVALS.md](EVALS.md) |
| Детермінований гейт у CI | ✅ `.github/workflows/eval.yml` | — |
| LLM-judge | ✅ п'ять вимірів, рубрика як дані | [ADR-0004](adr/0004-judge-rubric-as-data-with-versioning.md) |
| Демо тихої деградації | ✅ `make demo-degradation` | [EVALS.md](EVALS.md) |
| Langfuse | ✅ v3 self-host, деградує мовчки | [ADR-0008](adr/0008-langfuse-v3-self-host.md) |
| Slack | ✅ Socket Mode + файлова емуляція як фолбек | — |
| **Grafana MCP** | ❌ не використовується | [ADR-0001](adr/0001-direct-prometheus-loki-tools-instead-of-grafana-mcp.md) |
| **власний MCP `catalog`** | ❌ каталог — звичайні LangChain-тули | не зроблено, обґрунтування немає |
| **LangSmith для евалів** | ❌ замінено власним раннером + Langfuse | [EVALS.md](EVALS.md) |
| **reranker `bge-reranker-v2-m3`** | ❌ обрізано | [ADR-0002](adr/0002-e5-large-instead-of-minilm-for-kb-retrieval.md) |
| **`SummarizationMiddleware` при 80%** | ❌ не реалізовано | не зроблено, обґрунтування немає |
| **окрема identity агента, egress allowlist** | ❌ єдиний `admin:admin` на Grafana | не зроблено; частково закрито токеном на вході агента |
| **PR у git з правилами алертів** | ❌ обрізано згідно з порядком обрізання в брифі | — |
| **Дашборди як артефакт A3** | ❌ roadmap, як і планувалось | — |
| **Kubernetes (`kind`)** | ❌ події з файлу | [ADR-0009](adr/0009-no-kubernetes-on-the-stand.md) |

Відкриті питання з §8 закриті так: Slack — **реальний**; Kubernetes — **файл**;
евали — **власний раннер**, LangSmith не використовується; мова — **українська**.
