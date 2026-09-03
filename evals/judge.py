"""LLM-суддя: якісна оцінка звіту за рубрикою. Бігає на реліз, не на кожен коміт.

Три рішення, кожне з них — вибір, а не замовчування.

**Один виклик на вимір, а не один виклик на весь звіт.** Спокуса зекономити токенів,
згодувавши судді всі п'ять шкал одразу, коштує точності: у батчі виміри тягнуть один
одного (слабка обґрунтованість тягне вниз і корисність дій, хоча це різні речі).

**Rationale перед score.** Порядок ключів у відповіді зафіксовано у промпті й
перевіряється тут: оцінка, ВИВЕДЕНА з міркування, точніша за оцінку, обґрунтовану
після. Це різні речі, і різниця вимірна.

**n/a — не нуль.** Вимір без знаменника (кейс на відмову не має рекомендованих дій)
віддає {"na": "..."} і НЕ всереднюється. Інакше правильна поведінка агента карається:
чесна відмова опускала б середню actionability так само, як безпорадний звіт.
Структурний n/a (оголошений у eval.toml) навіть не витрачає виклик судді.
"""
from __future__ import annotations

import json
from typing import Any

from evals import config


class NotApplicable:
    """Вимір без знаменника. Окремий тип, щоб його не можна було випадково скласти з 0."""

    __slots__ = ("reason", "structural")

    def __init__(self, reason: str, structural: bool = False) -> None:
        self.reason = reason
        self.structural = structural

    def __repr__(self) -> str:
        return f"n/a({self.reason})"

    def as_dict(self) -> dict:
        return {"na": self.reason, "structural": self.structural}


class Score:
    """Оцінка виміру разом з міркуванням, з якого вона виведена."""

    __slots__ = ("value", "rationale")

    def __init__(self, value: float, rationale: str) -> None:
        self.value = value
        self.rationale = rationale

    def __repr__(self) -> str:
        return f"{self.value:.2f}"

    def as_dict(self) -> dict:
        return {"score": round(self.value, 3), "rationale": self.rationale}


def _artifacts(case: dict, report: Any, tool_log: str) -> str:
    limit = config.load()["judge"]["max_chars"]
    body = (
        f"ВХІД: {case['input']}\n"
        f"СЕРВІС: {case['service']}\n"
        f"ЕТАЛОННИЙ КЛАС ПРИЧИНИ: {case.get('expected_root_cause', '—')}\n"
        f"ОЧІКУВАНІ ТУЛИ: {case.get('expect_tools', [])}\n\n"
        f"ЗВІТ АГЕНТА:\n{report.model_dump_json(indent=2) if report else '(звіту немає)'}\n\n"
        f"ЛОГ ІНСТРУМЕНТІВ:\n{tool_log}"
    )
    if len(body) <= limit:
        return body
    # Обрізання гучне: суддя має знати, що судить неповний артефакт, інакше він
    # порахує відсутність доказу за його відсутність, а не за обрізання.
    return body[:limit] + f"\n\n[ОБРІЗАНО: артефакт довший за {limit} символів]"


def _parse(raw: str, dimension: str) -> Score | NotApplicable:
    """Розбір відповіді судді. Порядок ключів — частина контракту, тому перевіряється."""
    if "{" not in raw or "}" not in raw:
        raise ValueError(f"{dimension}: суддя відповів без JSON: {raw[:300]!r}")
    payload = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    keys = list(payload)
    if keys[:1] != ["rationale"]:
        raise ValueError(f"{dimension}: rationale має йти першим, отримано {keys}")

    score = payload["score"]
    if isinstance(score, dict) and "na" in score:
        return NotApplicable(str(score["na"]))
    value = float(score)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{dimension}: оцінка {value} поза 0.0-1.0")
    return Score(value, str(payload["rationale"]))


def score_dimension(name: str, case: dict, report: Any, tool_log: str,
                    model: str | None = None) -> Score | NotApplicable:
    """Один вимір — один виклик судді."""
    if not config.applicable(name, case):
        return NotApplicable(f"вимір не застосовний до кейса виду {case.get('kind')}",
                             structural=True)

    from agents.models import resolve

    prompt = f"{config.system_prompt()}\n\n---\n\n{config.dimension_prompt(name)}"
    answer = resolve(model or config.judge_model()).invoke([
        {"role": "system", "content": prompt},
        {"role": "user", "content": _artifacts(case, report, tool_log)},
    ])
    return _parse(str(answer.content), name)


def judge(case: dict, report: Any, tool_log: str,
          model: str | None = None) -> dict[str, dict]:
    """Усі застосовні виміри кейса. Гард рубрики — перед першим витраченим токеном."""
    config.check_rubric_integrity()
    return {
        name: score_dimension(name, case, report, tool_log, model).as_dict()
        for name in config.dimensions()
    }


def average(rows: list[dict], dimension: str) -> float | None:
    """Середнє по виміру. n/a не входять у знаменник — саме в цьому їхній сенс."""
    values = [r["judge"][dimension]["score"] for r in rows
              if "judge" in r and "score" in r["judge"].get(dimension, {})]
    return round(sum(values) / len(values), 3) if values else None
