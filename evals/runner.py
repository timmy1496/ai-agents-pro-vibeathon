"""Прогін кейсів датасету.

Два режими:
  offline — виконує очікувану траєкторію тулів на записаних виводах, без моделі й без
            грошей. Перевіряє, що тули працюють і що кейс розв'язний зі своїх доказів.
  online  — той самий кейс справжнім агентом; потрібен ANTHROPIC_API_KEY.
"""
from __future__ import annotations

from evals.cases import tool_args

# Який доказ робить кейс розв'язним для кожного класу причини. Якщо доказу немає —
# кейс або розмічений хибно, або нерозв'язний, і модель тут ні до чого.
DISCRIMINATORS = {
    "release": lambda ev: any(d["minutes_ago"] <= 15 for d in ev["deploys"]),
    "dependency": lambda ev: any("timeout" in p["pattern"].lower() for p in ev["patterns"]),
    "resources": lambda ev: any(e["reason"] in ("OOMKilling", "BackOff") for e in ev["k8s_events"]),
    "config": lambda ev: any("ConfigMap" in e["message"] or "probe" in e["message"].lower()
                             for e in ev["k8s_events"]),
    "capacity": lambda ev: ev["signals"]["rps"]["current_avg"] is not None,
    "unknown": lambda ev: not any(
        DISCRIMINATORS[label](ev) for label in ("release", "dependency", "resources")),
}


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
    """Чи є у зібраних доказах те, що відрізняє цей клас причини від інших."""
    return DISCRIMINATORS[case["expected_root_cause"]](evidence)


def run_online(case: dict, config: dict | None = None) -> dict:
    """Справжній агент на записаних виводах. Потребує ключа."""
    from agents.incident_agent import investigate

    result = investigate({"summary": case["input"], "service": case["service"]}, config=config)
    tool_messages = [m for m in result["state"]["messages"] if m.type == "tool"]
    tools_called = [m.name for m in tool_messages]
    if result["report"] is None:
        return {"case_id": case["id"], "tools_called": tools_called,
                "missing_tools": sorted(set(case.get("expect_tools", [])) - set(tools_called)),
                "report": None, "tool_log": "", "root_cause_match": False,
                "revisions": result["revisions"], "grounded": False, "error": result["error"]}
    return {
        "tool_log": "\n".join(f"[{m.name}] {m.content}" for m in tool_messages),
        "case_id": case["id"],
        "tools_called": tools_called,
        "missing_tools": sorted(set(case.get("expect_tools", [])) - set(tools_called)),
        "report": result["report"],
        "root_cause_match": result["report"].root_cause_label == case["expected_root_cause"],
        "revisions": result["revisions"],
        "grounded": result["verdict"].grounded,
    }
