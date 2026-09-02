"""Завантаження датасету і типові аргументи тулів для офлайн-відтворення траєкторії."""
from __future__ import annotations

import pathlib

import yaml

CASES_FILE = pathlib.Path(__file__).parent / "cases.yaml"

# Аргументи, з якими тул викликається в записаній траєкторії. Дублювати їх у кожному
# кейсі немає сенсу — для гейта важлива послідовність тулів, а не варіації параметрів.
DEFAULT_ARGS = {
    "get_service": lambda case: {"name": case["service"]},
    "get_active_alerts": lambda case: {"service": case["service"]},
    "golden_signals": lambda case: {"service": case["service"], "minutes": 30},
    "query_loki_patterns": lambda case: {"service": case["service"], "minutes": 30},
    "query_loki_logs": lambda case: {"service": case["service"], "contains": "error"},
    "query_prometheus": lambda case: {"query": f'up{{service="{case["service"]}"}}'},
    "get_deploys": lambda case: {"service": case["service"], "hours": 6},
    "k8s_events": lambda case: {"service": case["service"], "hours": 6},
    "similar_incidents": lambda case: {"symptoms": case["input"], "service": case["service"]},
    "search_kb": lambda case: {"query": case["input"]},
    "list_services": lambda case: {},
}


def load() -> list[dict]:
    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), f"дублікати id у датасеті: {ids}"
    return cases


def by_kind(kind: str) -> list[dict]:
    return [c for c in load() if c["kind"] == kind]


def tool_args(name: str, case: dict) -> dict:
    """Типові аргументи, перекриті тим, що кейс задав явно в args.<tool>."""
    return {**DEFAULT_ARGS[name](case), **case.get("args", {}).get(name, {})}
