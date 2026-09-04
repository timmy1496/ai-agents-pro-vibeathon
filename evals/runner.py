"""Прогін кейсів датасету.

Два режими:
  offline — виконує очікувану траєкторію тулів на записаних виводах, без моделі й без
            грошей. Перевіряє, що тули працюють і що кейс розв'язний зі своїх доказів.
  online  — той самий кейс справжнім агентом; потрібен ANTHROPIC_API_KEY.
"""
from __future__ import annotations

from evals.cases import tool_args

# Детермінований класифікатор доказів. Не для продакшену — це інструмент якості
# датасету: він відповідає на питання "на що взагалі вказують докази цього кейса".
# Раніше тут були незалежні предикати на клас, і вони перетиналися на грубих ключових
# словах (лог зі словом "cache" робив кейс одночасно і resources, і capacity). Порядок
# знімає перетини так само, як це робить жива тріаж-послідовність: спершу найбільш
# специфічний сигнал, потім слабші.
SATURATION_WORDS = ("cache", "queue", "consumer lag", "pool", "eviction")
DEPLOY_WINDOW_MINUTES = 15  # той самий, що й у промпті A2


def _has_oom(evidence: dict) -> bool:
    return any(e["reason"] in ("OOMKilling", "BackOff") for e in evidence["k8s_events"])


def _has_config_change(evidence: dict) -> bool:
    """Зміна конфігу або тріпотіння готовності — на метриках це виглядає як що завгодно."""
    events = evidence["k8s_events"]
    return (any(e["reason"] == "ConfigMapUpdated" for e in events)
            or sum(e["reason"] == "Unhealthy" for e in events) >= 2)


def _has_timeout_pattern(evidence: dict) -> bool:
    return any("timeout" in p["pattern"].lower() for p in evidence["patterns"])


def _has_recent_deploy(evidence: dict) -> bool:
    return any(d["minutes_ago"] <= DEPLOY_WINDOW_MINUTES for d in evidence["deploys"])


# Наскільки має вирости трафік, щоб слово про насичення означало саме capacity.
# Нижче за це "cache"/"pool"/"queue" у логах однаково добре пояснюються залежністю.
LOAD_PRESSURE = 1.5


def _has_saturation(evidence: dict) -> bool:
    """Насичення = сплеск трафіку, або ознака насичення ПІД навантаженням.

    Раніше достатньо було слова зі SATURATION_WORDS у топ-3 патернах — і саме ця
    коротка дорога сховала хибно розмічений кейс: cap-02 мав рівний трафік (88 -> 90)
    і мітку "capacity", класифікатор бачив "cache" у логах і погоджувався з міткою,
    тому test_case_is_solvable_from_its_own_evidence мовчав. Агент відповідав
    "dependency" — правильно — і отримував MISS.

    Слово про насичення без жодного росту навантаження не є ознакою capacity:
    масові cache miss при рівному трафіку — це поведінка залежності.
    """
    rps = evidence["signals"]["rps"]
    current, baseline = rps["current_avg"] or 0, rps["baseline_avg"]
    ratio = current / baseline if baseline else 0
    if ratio >= 3:
        return True
    pattern = any(word in p["pattern"].lower()
                  for p in evidence["patterns"][:3] for word in SATURATION_WORDS)
    return pattern and ratio >= LOAD_PRESSURE


# Порядок = специфічність сигналу. OOM іде перед деплоєм свідомо: leak, що приїхав
# релізом, лікується як проблема ресурсів, а не відкотом (див. kb/runbooks/oomkilled-restarts.md).
CLASSIFIER = (
    ("resources", _has_oom),
    ("config", _has_config_change),
    ("dependency", _has_timeout_pattern),
    ("release", _has_recent_deploy),
    ("capacity", _has_saturation),
)


def classify(evidence: dict) -> str:
    """Клас причини, на який вказують докази. 'unknown', якщо не вказують ні на що."""
    return next((label for label, fires in CLASSIFIER if fires(evidence)), "unknown")


def _acceptable(case: dict) -> set[str]:
    """Класи причини, які зараховуються. Інциденти бувають шаруваті: leak, що приїхав
    релізом, чесно описується і як 'resources', і як 'release' — датасет це визнає явно,
    замість того щоб карати агента за правильну відповідь."""
    return {case["expected_root_cause"], *case.get("acceptable_root_causes", [])}


def collect_evidence(case: dict) -> dict:
    """Виконує read-тули кейса і збирає докази — те саме, що побачить агент."""
    from agents.tools.observability import golden_signals, query_loki_patterns
    from agents.tools.stand import get_deploys, k8s_events

    service = case["service"]
    return {
        "signals": golden_signals.invoke({"service": service, "minutes": 30})["signals"],
        "patterns": query_loki_patterns.invoke({"service": service})["patterns"],
        "deploys": get_deploys.invoke({"service": service, "hours": 6}),
        "k8s_events": k8s_events.invoke({"service": service, "hours": 6}),
    }


def run_trajectory(case: dict) -> dict[str, object]:
    """Виконує очікувану послідовність тулів; повертає вивід кожного."""
    from agents.incident_agent import READ_TOOLS
    from agents.knowledge_agent import TOOLS as KB_TOOLS

    registry = {t.name: t for t in [*READ_TOOLS, *KB_TOOLS]}
    return {name: registry[name].invoke(tool_args(name, case))
            for name in case.get("expect_tools", [])}


def is_solvable(case: dict, evidence: dict) -> bool:
    """Чи вказують докази саме на очікуваний клас (або на визнану альтернативу)."""
    return classify(evidence) in _acceptable(case)


def covered_tools(called: list[str]) -> set[str]:
    """Які тули фактично покриті — з урахуванням композитів.

    `incident_snapshot` віддає за один виклик те, що раніше збиралося чотирма. Якщо
    рахувати самі імена, агент, який зібрав ті самі дані дешевше, виглядає гіршим за
    того, хто зробив чотири виклики. Датасет описує, які ДОКАЗИ потрібні кейсу, а
    якими викликами вони приїхали — деталь реалізації тул-шару.
    """
    from agents.tools.observability import COMPOSES

    covered = set(called)
    for name in called:
        covered.update(COMPOSES.get(name, ()))
    return covered


def run_online(case: dict, config: dict | None = None) -> dict:
    """Справжній агент на записаних виводах. Потребує ключа."""
    from agents.incident_agent import investigate

    result = investigate({"summary": case["input"], "service": case["service"]}, config=config)
    tool_messages = [m for m in result["state"]["messages"] if m.type == "tool"]
    tools_called = [m.name for m in tool_messages]
    covered = covered_tools(tools_called)
    if result["report"] is None:
        return {"case_id": case["id"], "tools_called": tools_called,
                "missing_tools": sorted(set(case.get("expect_tools", [])) - covered),
                "report": None, "tool_log": "", "root_cause_match": False,
                "revisions": result["revisions"], "critic_accepted": False,
                "critic_problems": [], "error": result["error"]}
    return {
        "tool_log": "\n".join(f"[{m.name}] {m.content}" for m in tool_messages),
        "case_id": case["id"],
        "tools_called": tools_called,
        "missing_tools": sorted(set(case.get("expect_tools", [])) - covered),
        "report": result["report"],
        "root_cause_match": result["report"].root_cause_label in _acceptable(case),
        "revisions": result["revisions"],
        "critic_accepted": result["verdict"].grounded,
        # Претензії критика лежать поруч навмисно: коли він і суддя розходяться,
        # питання «хто з них правий» має розв'язуватись доказами, а не авторитетом.
        "critic_problems": result["verdict"].problems,
        # видно в звіті: чи агент дійшов до висновку сам, чи його дотягнув запобіжник
        "fallback_synthesis": result.get("fallback_synthesis", False),
    }
