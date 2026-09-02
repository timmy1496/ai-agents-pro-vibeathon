"""Онлайн-прогін датасету справжнім агентом. Потребує ANTHROPIC_API_KEY.

    python -m evals.run                 # усі rca-кейси + judge
    python -m evals.run --no-judge      # тільки траєкторія і клас причини (дешевше)
    python -m evals.run --case rel-01

Пише звіт у evals/reports/ і друкує дельту до попереднього прогону — щоб тиха
деградація (змінили формат логів, підкрутили промпт) була видима, а не здогадувана.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import pytest

from evals import cases as dataset
from evals.backend import use_fixtures

REPORTS = pathlib.Path(__file__).parent / "reports"


def run_case(case: dict, with_judge: bool, tmp: pathlib.Path) -> dict:
    from evals.judge import judge
    from evals.runner import run_online

    monkeypatch = pytest.MonkeyPatch()
    prepared = dict(case, _tmp_slack=tmp / f"slack-{case['id']}.json")
    try:
        with use_fixtures(prepared, monkeypatch):
            result = run_online(prepared)
    finally:
        monkeypatch.undo()

    row = {k: v for k, v in result.items() if k not in ("report", "tool_log")}
    row["root_cause"] = result["report"].root_cause_label
    row["confidence"] = result["report"].confidence
    row["evidence_count"] = len(result["report"].evidence)
    if with_judge:
        verdict = judge(case, result["report"], result["tool_log"])
        row["judge"] = verdict.model_dump()
    return row


def summarise(rows: list[dict]) -> dict:
    total = len(rows)
    scored = [r["judge"] for r in rows if "judge" in r]
    return {
        "cases": total,
        "root_cause_accuracy": round(sum(r["root_cause_match"] for r in rows) / total, 3),
        "tool_recall": round(sum(not r["missing_tools"] for r in rows) / total, 3),
        "grounded_rate": round(sum(r["grounded"] for r in rows) / total, 3),
        "avg_revisions": round(sum(r["revisions"] for r in rows) / total, 2),
        **({"avg_correctness": round(sum(j["correctness"] for j in scored) / len(scored), 2),
            "avg_groundedness": round(sum(j["groundedness"] for j in scored) / len(scored), 2),
            "avg_actionability": round(sum(j["actionability"] for j in scored) / len(scored), 2)}
           if scored else {}),
    }


def previous_summary() -> dict | None:
    reports = sorted(REPORTS.glob("*.json"))
    return json.loads(reports[-1].read_text())["summary"] if reports else None


def print_delta(current: dict, previous: dict | None) -> None:
    print("\n=== Підсумок ===")
    for key, value in current.items():
        if previous is None or key not in previous or not isinstance(value, (int, float)):
            print(f"  {key:22} {value}")
            continue
        delta = value - previous[key]
        mark = "=" if abs(delta) < 1e-9 else ("+" if delta > 0 else "")
        print(f"  {key:22} {value}   ({mark}{delta:+.3f} до попереднього)".replace("(+", "("))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="прогнати один кейс за id")
    parser.add_argument("--no-judge", action="store_true", help="без LLM-judge")
    args = parser.parse_args(argv)

    selected = [c for c in dataset.by_kind("rca")
                if args.case is None or c["id"] == args.case]
    if not selected:
        print(f"кейса '{args.case}' немає в датасеті", file=sys.stderr)
        return 2

    import os

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Потрібен ANTHROPIC_API_KEY у .env — онлайн-прогін кличе модель.\n"
              "Детермінований гейт бігає без ключа: make eval", file=sys.stderr)
        return 2

    tmp = REPORTS / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in selected:
        row = run_case(case, not args.no_judge, tmp)
        rows.append(row)
        status = "ok" if row["root_cause_match"] else "MISS"
        print(f"[{status:4}] {row['case_id']:8} причина={row['root_cause']:11} "
              f"пропущені тули={row['missing_tools'] or '-'}")

    summary = summarise(rows)
    previous = previous_summary()
    REPORTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (REPORTS / f"{stamp}.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False))
    print_delta(summary, previous)
    print(f"\nзвіт: evals/reports/{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
