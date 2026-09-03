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


class Unscored:
    """Вимір, який не вдалося виміряти. Це НЕ n/a і НЕ нуль.

    n/a означає «знаменника немає» — правильний стан. Unscored означає «знаменник є,
    але інструмент зламався»: суддя відповів прозою замість JSON, обірвався виклик.
    Плутати їх не можна в жодну сторону: як нуль це занизило б оцінку агента, як n/a —
    сховало б поломку вимірювання за чистою метрикою.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"unscored({self.reason})"

    def as_dict(self) -> dict:
        return {"error": self.reason}


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


REMINDER = ('\n\n[СИСТЕМА] Попередня відповідь була не за контрактом. Поверни РІВНО '
            'один JSON-об\'єкт {"rationale": …, "score": …} і нічого крім нього: '
            'без markdown, без заголовків, без тексту поза JSON.')


def score_dimension(name: str, case: dict, report: Any, tool_log: str,
                    model: str | None = None) -> Score | NotApplicable | Unscored:
    """Один вимір — один виклик судді, з однією повторною спробою на порушення контракту.

    Суддя час від часу відповідає прозою з markdown-заголовками замість JSON. Без
    ретраю це коштувало цілого кейса: агент відпрацював, звіт є, а рядок прогону
    втрачався на кроці вимірювання.
    """
    if not config.applicable(name, case):
        return NotApplicable(f"вимір не застосовний до кейса виду {case.get('kind')}",
                             structural=True)

    from agents.models import resolve

    grader = resolve(model or config.judge_model())
    system = f"{config.system_prompt()}\n\n---\n\n{config.dimension_prompt(name)}"
    artifacts = _artifacts(case, report, tool_log)

    for attempt_text in (artifacts, artifacts + REMINDER):
        answer = grader.invoke([{"role": "system", "content": system},
                                {"role": "user", "content": attempt_text}])
        try:
            return _parse(str(answer.content), name)
        except (ValueError, json.JSONDecodeError) as error:
            last = error
    return Unscored(f"суддя двічі відповів не за контрактом: {last}"[:300])


def judge(case: dict, report: Any, tool_log: str,
          model: str | None = None) -> dict[str, dict]:
    """Усі застосовні виміри кейса. Гард рубрики — перед першим витраченим токеном.

    Збій одного виміру не забирає інші й не забирає кейс: агент свою роботу вже зробив,
    і детерміновані метрики по ньому лишаються дійсними.
    """
    config.check_rubric_integrity()
    scores = {}
    for name in config.dimensions():
        try:
            scores[name] = score_dimension(name, case, report, tool_log, model).as_dict()
        except Exception as error:  # noqa: BLE001 — обрив виклику теж не має валити кейс
            scores[name] = Unscored(f"{type(error).__name__}: {error}"[:300]).as_dict()
    return scores


def unscored(rows: list[dict]) -> int:
    """Скільки вимірів не вдалося виміряти. Нуль у знаменнику надійності прогону."""
    return sum(1 for r in rows for entry in (r.get("judge") or {}).values()
               if "error" in entry)


def average(rows: list[dict], dimension: str) -> float | None:
    """Середнє по виміру. n/a не входять у знаменник — саме в цьому їхній сенс."""
    values = [r["judge"][dimension]["score"] for r in rows
              if "judge" in r and "score" in r["judge"].get(dimension, {})]
    return round(sum(values) / len(values), 3) if values else None
