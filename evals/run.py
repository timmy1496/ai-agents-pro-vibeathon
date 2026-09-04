"""Онлайн-прогін датасету справжнім агентом. Потребує ANTHROPIC_API_KEY або OPENROUTER_API_KEY.

    python -m evals.run                 # усі rca-кейси + суддя
    python -m evals.run --no-judge      # тільки траєкторія і клас причини (дешевше)
    python -m evals.run --case rel-01
    python -m evals.run --html          # ще й docs/eval-report.html

Пише звіт у evals/reports/ і друкує дельту до попереднього прогону — щоб тиха
деградація (змінили формат логів, підкрутили промпт) була видима, а не здогадувана.

Коди виходу: 0 — гейт зелений; 1 — пробито поріг; 2 — прогнати неможливо (немає ключа,
поїхала рубрика). Невідоме ніколи не проходить за зелене.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import pytest

from evals import cases as dataset
from evals import config, judge as judging
from evals.backend import use_fixtures

REPORTS = pathlib.Path(__file__).parent / "reports"
DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"


def run_case(case: dict, with_judge: bool, tmp: pathlib.Path) -> dict:
    """Один кейс. Виняток тут — результат кейса, а не аварія прогону.

    Прогін коштує десятки хвилин і сотні викликів; падати на сьомому кейсі з чотирнадцяти
    і втрачати вже зроблену роботу — найдорожчий спосіб дізнатись, що агент десь зламався.
    Кейс, який не доїхав, чесно рахується як провал (root_cause_match=False,
    grounded=False) і несе причину в рядку звіту — це видно і в підсумку, і в HTML.
    """
    try:
        return _run_case(case, with_judge, tmp)
    except Exception as error:  # noqa: BLE001 — саме тут ловимо все
        return {
            "case_id": case["id"], "root_cause": "—", "root_cause_match": False,
            "tools_called": [], "missing_tools": sorted(case.get("expect_tools", [])),
            "revisions": 0, "grounded": False,
            "error": f"{type(error).__name__}: {error}"[:500],
        }


def _run_case(case: dict, with_judge: bool, tmp: pathlib.Path) -> dict:
    from evals.runner import run_online

    monkeypatch = pytest.MonkeyPatch()
    prepared = dict(case, _tmp_slack=tmp / f"slack-{case['id']}.json")
    try:
        with use_fixtures(prepared, monkeypatch):
            result = run_online(prepared)
    finally:
        monkeypatch.undo()

    row = {k: v for k, v in result.items() if k not in ("report", "tool_log")}
    if result["report"] is None:
        row["root_cause"] = "—"
        return row
    row["root_cause"] = result["report"].root_cause_label
    row["confidence"] = result["report"].confidence
    row["evidence_count"] = len(result["report"].evidence)
    row["hypothesis"] = result["report"].hypothesis
    row["recommended_actions"] = result["report"].recommended_actions
    if with_judge:
        row["judge"] = judging.judge(case, result["report"], result["tool_log"])
    return row


def summarise(rows: list[dict]) -> dict:
    """Детерміновані метрики + середні по вимірах судді (n/a у знаменник не входять)."""
    total = len(rows)
    summary = {
        "cases": total,
        "root_cause_accuracy": round(sum(r["root_cause_match"] for r in rows) / total, 3),
        "tool_recall": round(sum(not r["missing_tools"] for r in rows) / total, 3),
        # Не "grounded_rate": ця метрика ніколи не міряла обґрунтованість звіту — вона
        # міряє ВЕРДИКТ КРИТИКА. Різниця перестала бути академічною на прогоні
        # 2026-09-04: критик відхилив два звіти, спаливши по два оберти, а незалежний
        # суддя дав тим самим звітам groundedness 0.85 і 1.0. Обґрунтованість міряє
        # суддя (вимір `groundedness`); тут — скільки звітів пройшли критика з першого
        # разу, тобто наскільки дорогий і наскільки шумний сам критик.
        "critic_accept_rate": round(sum(r["critic_accepted"] for r in rows) / total, 3),
        "self_completed": round(sum(not r.get("fallback_synthesis") for r in rows) / total, 3),
        "avg_revisions": round(sum(r["revisions"] for r in rows) / total, 2),
    }
    for dimension in config.dimensions():
        value = judging.average(rows, dimension)
        if value is not None:
            summary[dimension] = value

    attempted = sum(1 for r in rows for entry in (r.get("judge") or {}).values()
                    if "score" in entry or "error" in entry)
    if attempted:
        summary["unscored_rate"] = round(judging.unscored(rows) / attempted, 3)
    return summary


def check_gate(summary: dict, previous: dict | None) -> list[str]:
    """Абсолютні пороги + падіння проти попереднього прогону. Пороги — з eval.toml.

    Порівняння з попереднім прогоном свідомо окреме від абсолютних порогів: метрика
    може стояти вище порогу і при цьому впасти на 0.2 за коміт — це теж регресія,
    просто ще не аварія.
    """
    thresholds = config.gate()
    failures = [
        f"{metric} = {summary[metric]:.3f} нижче порогу {thresholds[key]:.2f}"
        for key, metric in (("min_root_cause_accuracy", "root_cause_accuracy"),
                            ("min_tool_recall", "tool_recall"),
                            ("min_critic_accept_rate", "critic_accept_rate"),
                            ("min_self_completed", "self_completed"),
                            ("min_correctness", "correctness"),
                            ("min_groundedness", "groundedness"),
                            ("min_actionability", "actionability"))
        if metric in summary and summary[metric] < thresholds[key]
    ]
    # Окремо і в інший бік: тут погано, коли ВИЩЕ порогу. Це не оцінка агента, а
    # надійність вимірювання — зелена метрика, порахована по половині кейсів, гірша
    # за чесно червону.
    if summary.get("unscored_rate", 0) > thresholds["max_unscored"]:
        failures.append(
            f"не виміряно {summary['unscored_rate']:.1%} вимірів при стелі "
            f"{thresholds['max_unscored']:.0%} — метрикам цього прогону вірити не можна")
    if previous:
        failures += [
            f"{metric} впав на {previous[metric] - summary[metric]:.3f} "
            f"(було {previous[metric]:.3f}, стало {summary[metric]:.3f})"
            for metric in summary
            if isinstance(summary.get(metric), float) and metric in previous
            and previous[metric] - summary[metric] > thresholds["max_drop"]
        ]
    return failures


def comparable(meta: dict) -> bool:
    """Чи можна порівнювати цей прогін з поточним.

    П'ять речей мають збігтись, і всі п'ять — частини вимірювального інструмента, а не
    налаштування: версія рубрики, модель судді, ПРОВАЙДЕР, набір кейсів і ВМІСТ датасету.

    Провайдер тут не для повноти: через Claude Code CLI tool calling промптовий, а не
    нативний, тобто агент виконує іншу механіку виклику інструментів. Дельта між
    провайдерами виміряла б різницю транспортів, а не якість агента.

    Набір кейсів — з тієї ж причини. `--case rel-01` і `--case rel-02` дають по одному
    числу кожен, і різниця між ними це різниця кейсів, а не зміна агента. Раніше вони
    порівнювались між собою і друкували дельту, яка нічого не означала.

    Вміст датасету — окремо від набору id, бо кейс може лишитись собою за іменем і
    змінитись за суттю: cap-02 лишився cap-02, коли його фікстури переписали. Дельта
    через таку правку показала б зміну агента там, де змінилось завдання.
    """
    from agents.models import provider

    return (meta.get("rubric_version") == config.rubric_version()
            and meta.get("judge_model") == config.judge_model()
            and meta.get("provider") == provider()
            and meta.get("case_ids") == _case_ids
            and meta.get("dataset_sha") == config.dataset_digest())


_case_ids: list[str] = []


def previous_report() -> dict | None:
    """Останній ПОРІВНЯННИЙ прогін. Решта пропускається, а не приводиться до вигляду."""
    for path in sorted(REPORTS.glob("*.json"), reverse=True):
        report = json.loads(path.read_text(encoding="utf-8"))
        if comparable(report.get("meta", {})):
            return report
    return None


def print_delta(current: dict, previous: dict | None) -> None:
    print("\n=== Підсумок ===")
    for key, value in current.items():
        if previous is None or key not in previous or not isinstance(value, (int, float)):
            print(f"  {key:22} {value}")
            continue
        delta = value - previous[key]
        change = "без змін" if abs(delta) < 1e-9 else f"{delta:+.3f} до попереднього"
        print(f"  {key:22} {value}   ({change})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", metavar="ID",
                        help="прогнати лише ці кейси (можна кілька разів)")
    parser.add_argument("--no-judge", action="store_true", help="без LLM-судді")
    parser.add_argument("--html", action="store_true", help="ще й docs/eval-report.html")
    args = parser.parse_args(argv)

    try:  # fail-closed ДО витрати токенів: зіпсована рубрика не має що вимірювати
        config.check_rubric_integrity()
    except config.RubricDrift as drift:
        print(f"рубрика поїхала:\n{drift}", file=sys.stderr)
        return 2

    selected = [c for c in dataset.by_kind("rca")
                if not args.case or c["id"] in args.case]
    if not selected:
        print(f"таких кейсів немає в датасеті: {args.case}", file=sys.stderr)
        return 2
    if args.case and (unknown := set(args.case) - {c["id"] for c in selected}):
        print(f"невідомі id пропущено: {sorted(unknown)}", file=sys.stderr)

    from agents.config import STRONG_MODEL  # імпорт тягне load_dotenv
    from agents.models import provider, reset_spend, spend

    transport = provider()
    if transport == "claude-code":
        from agents.providers import claude_code

        if not claude_code.available():
            print("Немає ні API-ключа, ні Claude Code CLI у PATH.\n"
                  "Детермінований гейт бігає без обох: make eval", file=sys.stderr)
            return 2
        print("провайдер: Claude Code CLI (підписка). Tool calling промптовий, не "
              "нативний — оцінки не порівнюються з прогонами через API.\n")
    reset_spend()

    tmp = REPORTS / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    _case_ids[:] = [c["id"] for c in selected]
    rows = []
    print(f"кейсів: {len(selected)}\n", flush=True)
    for case in selected:
        row = run_case(case, not args.no_judge, tmp)
        rows.append(row)
        status = "ok" if row["root_cause_match"] else ("ERR" if row.get("error") else "MISS")
        print(f"[{status:4}] {row['case_id']:8} причина={row['root_cause']:11} "
              f"пропущені тули={row['missing_tools'] or '-'}"
              + (f"\n       {row['error']}" if row.get("error") else ""), flush=True)

    summary = summarise(rows)
    previous = previous_report()
    failures = check_gate(summary, (previous or {}).get("summary"))
    report = {
        "meta": {
            "stamp": dt.datetime.now().isoformat(timespec="seconds"),
            "rubric_version": config.rubric_version(),
            "prompts_sha": config.prompts_digest(),
            "judge_model": config.judge_model() if not args.no_judge else None,
            "agent_model": STRONG_MODEL,
            "provider": transport,
            "cases": len(rows),
            "case_ids": _case_ids,
            "dataset_sha": config.dataset_digest(),
            "spend": spend(),
        },
        "summary": summary,
        "gate": {"passed": not failures, "failures": failures},
        "rows": rows,
    }

    REPORTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (REPORTS / f"{stamp}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print_delta(summary, (previous or {}).get("summary"))
    if (cost := spend()):
        per_case = cost["usd"] / max(len(rows), 1)
        print(f"\n  {'викликів моделі':22} {cost['calls']}")
        print(f"  {'вартість (прейскурант)':22} ${cost['usd']:.2f} "
              f"(${per_case:.3f} на кейс)")
    print(f"\nзвіт: evals/reports/{stamp}.json")

    if args.html:
        from evals.report import write_html

        path = write_html(report, previous, DOCS / "eval-report.html")
        print(f"html:  {path.relative_to(DOCS.parent)}")

    if failures:
        print("\n=== ГЕЙТ ЧЕРВОНИЙ ===", file=sys.stderr)
        for failure in failures:
            print(f"  • {failure}", file=sys.stderr)
        return 1
    print("\nгейт зелений")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
