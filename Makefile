.PHONY: up down ps logs incident-1 incident-2 incident-3 reset selfcheck urls doctor doctor-warm demo

up:            ## Підняти стенд
	docker compose up -d --build
	@$(MAKE) --no-print-directory urls

down:          ## Зупинити стенд і прибрати томи
	docker compose down -v

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

incident-1:    ## Деплой -> 5xx (root cause: реліз)
	./scripts/incident.sh 1

incident-2:    ## orders-db down -> latency/timeouts (root cause: залежність)
	./scripts/incident.sh 2

incident-3:    ## Memory leak -> OOMKilled (root cause: ресурси)
	./scripts/incident.sh 3

reset:         ## Зняти chaos і перезапустити демо-сервіс
	./scripts/incident.sh reset

doctor:        ## Чи готовий стенд до демо (правила, логи, KB, токен, ключ)
	.venv/bin/python -m scripts.doctor

doctor-warm:   ## Те саме + прогріти ембеддер і індекс KB (перед демо — обов'язково)
	.venv/bin/python -m scripts.doctor --warm

demo:          ## Наскрізний сценарій демо: 5 агентів, HITL, евали, тиха деградація
	./scripts/demo.sh

selfcheck:     ## Self-check chaos-svc без стенду
	cd services/chaos-svc && python3 -m pip install -q -r requirements.txt httpx && python3 app.py

urls:
	@echo "Grafana       http://localhost:3000 (admin/admin)"
	@echo "Prometheus    http://localhost:9090"
	@echo "Alertmanager  http://localhost:9093"
	@echo "Loki          http://localhost:3100"
	@echo "Qdrant        http://localhost:6333/dashboard"
	@echo "chaos-svc     http://localhost:8080"
	@echo "Langfuse      http://localhost:3001 (demo@example.com / demodemo123)"
	@echo "Агент         http://localhost:8000 (треди) — make agent"
	@echo "Дашборд       http://localhost:3000/d/sre-agent-golden"
	@echo ""
	@echo "Перед демо:   make doctor-warm"

.PHONY: embed-serve embed-check embed-bench
embed-serve:   ## Ембеддер KB локально: nomic-embed-text-v2-moe через llama-server на :8081 (ADR-0010)
	llama-server -hf nomic-ai/nomic-embed-text-v2-moe-GGUF:Q8_0 --embedding --pooling mean \
	  --ctx-size 512 --ubatch-size 512 --batch-size 512 --host 127.0.0.1 --port 8081

embed-check:   ## Чи відповідає ембеддер: /health і два вектори по 768 з /v1/embeddings
	@curl -sf localhost:8081/health && echo
	@curl -sf localhost:8081/v1/embeddings -H 'Content-Type: application/json' \
	  -d '{"input": ["search_query: інциденти з OOMKilled", "search_document: # Runbook: OOMKilled"]}' \
	  | python3 -c "import sys,json; d=json.load(sys.stdin); print('vectors:', len(d['data']), 'dims:', len(d['data'][0]['embedding']), 'tokens:', d['usage']['prompt_tokens'])"

embed-bench:   ## Замір ембеддера на KB: hit@k і щілина релевантне/стороннє (числа ADR-0010)
	.venv/bin/python -m scripts.embed_bench $(EMBED_BENCH_ARGS)

.PHONY: install kb-index test
install:       ## Віртуальне оточення і залежності
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt

kb-index:      ## Переіндексувати KB у Qdrant стенду
	.venv/bin/python -m agents.kb.store

test:          ## Офлайн-тести (Qdrant у режимі :memory:, без docker)
	.venv/bin/python -m pytest tests -q

.PHONY: eval eval-contract eval-online eval-report eval-rubric-bump
eval:          ## Детермінований гейт евалів (без ключа, без стенду)
	.venv/bin/python -m pytest tests/test_eval_gate.py tests/test_eval_contract.py -q

eval-contract: ## Контракт вимірювального інструмента: версія рубрики, промпти, форма датасету
	.venv/bin/python -m pytest tests/test_eval_contract.py -q

eval-online:   ## Прогін датасету агентом + суддя за рубрикою (ключ АБО підписка Claude Code)
	.venv/bin/python -m evals.run --html

eval-report:   ## Перезібрати docs/eval-report.html з останнього JSON-звіту
	.venv/bin/python -m evals.report

eval-rubric-bump:  ## Бампнути версію рубрики після правки evals/prompts/*.md
	.venv/bin/python -m scripts.bump_rubric

.PHONY: agent
agent:         ## Запустити агента (вебхук Alertmanager + перегляд тредів на :8000)
	.venv/bin/uvicorn agents.app:app --host 0.0.0.0 --port 8000

.PHONY: demo-degradation
demo-degradation:  ## Демо тихої деградації: змінили формат логів -> гейт червоніє
	.venv/bin/python -m pytest tests/test_degradation.py -v

.PHONY: slack-check
slack-check:   ## Перевірити Slack: токен, канал, права, тред
	.venv/bin/python -m scripts.slack_check

.PHONY: slack-bot
slack-bot:     ## Слухати Slack через Socket Mode (згадка бота -> відповідь у тред)
	.venv/bin/python -m agents.slack_bot
