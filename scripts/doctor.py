"""Перевірка готовності стенду. Запускати ДО демо, а не під час нього.

Кожна перевірка відповідає на питання «чи спрацює наступний крок демо», і на кожну
є конкретна порада, що робити. Це не health-check контейнерів — docker і так скаже,
що вони живі; тут перевіряється те, що ламається тихо:

  * правила алертів завантажені (файл змонтований, але з синтаксичною помилкою —
    Prometheus стартує зеленим і просто не має правил);
  * у Loki взагалі є логи демо-сервісу (promtail мовчки не матчить мітки);
  * колекція KB існує І НЕПОРОЖНЯ — окремий випадок, який уже стріляв: попередній
    прогін створив колекцію і впав на записі, а агент упевнено відповідав «у базі немає»;
  * ембеддер прогрітий — інакше перший RCA на демо стоїть кілька хвилин, качаючи
    2 ГБ ONNX-моделі, і виглядає це як зависання;
  * агент піднятий і відповідає на токен, яким його кличе Alertmanager.

    python -m scripts.doctor            # перевірити
    python -m scripts.doctor --warm     # ще й прогріти: індекс KB + ембеддер
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

OK, FAIL, WARN = "✓", "✗", "!"


class Check:
    """Результат однієї перевірки. Порада — обов'язкова частина провалу, не додаток."""

    def __init__(self, name: str, passed: bool, detail: str = "", fix: str = "",
                 critical: bool = True) -> None:
        self.name, self.passed, self.detail, self.fix = name, passed, detail, fix
        self.critical = critical

    @property
    def glyph(self) -> str:
        return OK if self.passed else (FAIL if self.critical else WARN)

    def render(self) -> str:
        line = f"  {self.glyph} {self.name:38} {self.detail}"
        return line if self.passed else f"{line}\n      → {self.fix}"


def _get(url: str, timeout: int = 5) -> dict | list:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _reachable(name: str, url: str, hint: str, critical: bool = True) -> Check:
    try:  # HTTPError — підклас OSError, тому одного except достатньо
        urllib.request.urlopen(url, timeout=5).read()
    except OSError as error:
        return Check(name, False, "", f"{hint} ({error})", critical=critical)
    return Check(name, True, url)


def check_alert_rules(prometheus: str) -> Check:
    """Правило з одруком не валить Prometheus — воно просто зникає."""
    try:
        payload = _get(f"{prometheus}/api/v1/rules?type=alert")
    except OSError as error:
        return Check("правила алертів", False, "", f"Prometheus недоступний: {error}")

    names = [rule["name"] for group in payload["data"]["groups"]
             for rule in group["rules"] if rule.get("type") == "alerting"]
    expected = {"HighErrorRate", "HighLatencyP95", "FrequentRestarts", "HighMemoryUsage"}
    missing = expected - set(names)
    if missing:
        return Check("правила алертів", False, f"завантажено {len(names)}",
                     f"немає правил {sorted(missing)} — перевір синтаксис "
                     f"infra/prometheus/rules/alerts.yml і зроби "
                     f"`curl -X POST {prometheus}/-/reload`")
    return Check("правила алертів", True, f"{len(names)} правил, усі golden signals")


def check_metrics(prometheus: str, service: str) -> Check:
    try:
        payload = _get(f"{prometheus}/api/v1/query?query=up%7Bservice=%22{service}%22%7D")
    except OSError as error:
        return Check("метрики демо-сервісу", False, "", f"Prometheus недоступний: {error}")
    result = payload.get("data", {}).get("result", [])
    if not result:
        return Check("метрики демо-сервісу", False, "",
                     f"Prometheus не скрейпить {service}: перевір scrape_configs "
                     f"і чи піднявся контейнер (`docker compose ps chaos-svc`)")
    return Check("метрики демо-сервісу", True, f"up{{service={service}}} = {result[0]['value'][1]}")


def check_logs(loki: str, service: str) -> Check:
    """promtail мовчки не матчить мітки — і Loki просто порожній."""
    query = urllib.parse.quote(f'{{service="{service}"}}')
    try:
        payload = _get(f"{loki}/loki/api/v1/query_range?query={query}&limit=5")
    except OSError as error:
        return Check("логи в Loki", False, "", f"Loki недоступний: {error}")
    streams = payload.get("data", {}).get("result", [])
    lines = sum(len(s.get("values", [])) for s in streams)
    if not lines:
        return Check("логи в Loki", False, "порожньо",
                     f"promtail не приносить логи {service}: перевір мітку "
                     f"`sre.service` на контейнері і relabel_configs у "
                     f"infra/promtail/promtail.yml")
    return Check("логи в Loki", True, f"{lines} рядків у вибірці")


def check_kb() -> Check:
    """Колекція є, але порожня — окремий стан, і саме він виглядає як «у базі немає»."""
    from agents.config import KB_COLLECTION
    from agents.kb import store

    try:
        client = store.client()
        if not client.collection_exists(KB_COLLECTION):
            return Check("KB у Qdrant", False, "колекції немає", "make kb-index")
        count = client.count(KB_COLLECTION).count
    except Exception as error:  # клієнт кидає власні типи помилок
        return Check("KB у Qdrant", False, "", f"Qdrant недоступний: {error}")

    if count == 0:
        return Check("KB у Qdrant", False, "колекція порожня",
                     "колекцію створено, але не заповнено — попередній прогін упав "
                     "на записі. Агент у цьому стані впевнено каже «у базі немає». "
                     "make kb-index")
    return Check("KB у Qdrant", True, f"{count} чанків")


def check_embedder() -> Check:
    """2 ГБ ONNX качаються один раз — але робити це посеред демо не варто.

    Шукаємо саме модель, а не просто непорожню теку: fastembed створює теку одразу,
    тому «тека існує» не означає нічого. І шукаємо за ХВОСТОМ імені моделі, а не за
    повним: у кеші вона лежить під ім'ям репозиторію, з якого приїхала
    (intfloat/multilingual-e5-large -> models--qdrant--multilingual-e5-large-onnx),
    тому пошук за повним ім'ям давав хибне «не в кеші» на прогрітій машині.
    """
    from agents.config import DENSE_MODEL, FASTEMBED_CACHE

    wanted = DENSE_MODEL.rsplit("/", 1)[-1].lower()
    cached = ([d for d in FASTEMBED_CACHE.glob("*") if wanted in d.name.lower()]
              if FASTEMBED_CACHE.exists() else [])
    if cached:
        return Check("ембеддер прогрітий", True, str(cached[0]))
    return Check("ембеддер прогрітий", False, f"{DENSE_MODEL} не в кеші {FASTEMBED_CACHE}",
                 "перший RCA стоятиме кілька хвилин, качаючи модель: "
                 "`make doctor-warm`", critical=False)


def warm_up() -> None:
    """Прогрів ДО перевірок, а не серед них: інакше звіт описує стан, який щойно змінився."""
    from agents.kb import store

    print("прогріваю ембеддер і індексую KB (перший раз ~2 ГБ, кілька хвилин)…")
    store.reindex()


def check_agent(agent_url: str, token: str) -> Check:
    """Той самий токен, яким агента кличе Alertmanager."""
    try:
        request = urllib.request.Request(
            f"{agent_url}/webhook/alert", method="POST",
            data=json.dumps({"alerts": []}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        urllib.request.urlopen(request, timeout=5).read()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return Check("агент приймає вебхук", False, "401",
                         "AGENT_TOKEN у .env не збігається з credentials у "
                         "infra/alertmanager/alertmanager.yml — Alertmanager отримає "
                         "те саме 401 і алерт до агента не дійде")
        return Check("агент приймає вебхук", False, f"HTTP {error.code}", str(error))
    except OSError as error:
        return Check("агент приймає вебхук", False, "", f"агент не піднятий: `make agent` ({error})")
    return Check("агент приймає вебхук", True, agent_url)


def check_model_access() -> Check:
    """Чи є чим ходити в модель. Ключ — не єдиний варіант."""
    from agents.models import provider

    transport = provider()
    if transport == "claude-code":
        from agents.providers import claude_code

        if claude_code.available():
            return Check("доступ до моделі", True,
                         "Claude Code CLI (підписка); tool calling промптовий")
        return Check("доступ до моделі", False, "ні ключа, ні CLI",
                     "додай ANTHROPIC_API_KEY / OPENROUTER_API_KEY у .env або встанови "
                     "Claude Code CLI — без цього працює лише детермінована половина "
                     "(make eval, make test)")
    return Check("доступ до моделі", True, transport)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Готовність стенду до демо")
    parser.add_argument("--warm", action="store_true", help="прогріти індекс KB і ембеддер")
    parser.add_argument("--service", default="demo-chaos-svc")
    args = parser.parse_args(argv)

    from agents.config import (
        AGENT_TOKEN, ALERTMANAGER_URL, GRAFANA_URL, LANGFUSE_HOST, LOKI_URL, PROMETHEUS_URL,
    )

    if args.warm:
        warm_up()

    agent_url = "http://localhost:8000"
    checks = [
        _reachable("Prometheus", f"{PROMETHEUS_URL}/-/ready", "make up"),
        _reachable("Alertmanager", f"{ALERTMANAGER_URL}/-/ready", "make up"),
        _reachable("Loki", f"{LOKI_URL}/ready", "make up"),
        _reachable("Grafana", f"{GRAFANA_URL}/api/health", "make up"),
        check_alert_rules(PROMETHEUS_URL),
        check_metrics(PROMETHEUS_URL, args.service),
        check_logs(LOKI_URL, args.service),
        check_kb(),
        check_embedder(),
        check_model_access(),
        check_agent(agent_url, AGENT_TOKEN),
        # Langfuse не критичний: трейсинг деградує мовчки і інцидент не ламає (див.
        # agents/observability.py). Але на демо його показують, тому перевіряємо.
        _reachable("Langfuse", f"{LANGFUSE_HOST}/api/public/health",
                   "трейсів на демо не буде: `docker compose up -d langfuse`",
                   critical=False),
    ]

    print("\nГотовність стенду\n")
    for check in checks:
        print(check.render())

    broken = [c for c in checks if not c.passed and c.critical]
    warnings = [c for c in checks if not c.passed and not c.critical]
    print()
    if broken:
        print(f"{FAIL} демо не поїде: {len(broken)} критичних проблем\n")
        return 1
    if warnings:
        print(f"{WARN} демо поїде, але {len(warnings)} зауважень вище варто закрити\n")
    else:
        print(f"{OK} стенд готовий\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
