.PHONY: up down ps logs incident-1 incident-2 incident-3 reset selfcheck urls

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

selfcheck:     ## Self-check chaos-svc без стенду
	cd services/chaos-svc && python3 -m pip install -q -r requirements.txt httpx && python3 app.py

urls:
	@echo "Grafana       http://localhost:3000 (admin/admin)"
	@echo "Prometheus    http://localhost:9090"
	@echo "Alertmanager  http://localhost:9093"
	@echo "Loki          http://localhost:3100"
	@echo "Qdrant        http://localhost:6333/dashboard"
	@echo "chaos-svc     http://localhost:8080"
	@echo "Langfuse      http://localhost:3001 (demo@local / demodemo123)"

.PHONY: install kb-index test
install:       ## Віртуальне оточення і залежності
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt

kb-index:      ## Переіндексувати KB у Qdrant стенду
	.venv/bin/python -m agents.kb.store

test:          ## Офлайн-тести (Qdrant у режимі :memory:, без docker)
	.venv/bin/python -m pytest tests -q

.PHONY: eval eval-online
eval:          ## Детермінований гейт евалів (без ключа, без стенду)
	.venv/bin/python -m pytest tests/test_eval_gate.py -q

eval-online:   ## Прогін датасету справжнім агентом + LLM-judge (потрібен ANTHROPIC_API_KEY)
	.venv/bin/python -m evals.run
