"""Читання декларативної половини евалу і гарди на цілісність рубрики.

Розділення тут не косметичне. Промпти судді — це і є eval: правка `prompts/*.md`
змінює вимірювальний інструмент, а не налаштування. Тому все, що описує рубрику,
лежить у даних (`eval.toml` + markdown), а Python лише зчитує і механічно стежить,
щоб версія рубрики не розійшлась з її вмістом.

Обидва гарди fail-closed і стоять ДО витрати токенів: зіпсовану рубрику краще
не запускати взагалі, ніж отримати оцінки, які ні з чим не порівняти.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import pathlib
import tomllib

EVALS = pathlib.Path(__file__).parent
CONFIG_FILE = EVALS / "eval.toml"
PROMPTS_DIR = EVALS / "prompts"
HISTORY_FILE = EVALS / "rubric-history.json"

SYSTEM_PROMPT_FILE = "judge-system.md"
# Кожен промпт виміру мусить сказати судді, коли вимір незастосовний. Без цього рядка
# суддя ставить 0 там, де немає знаменника, і n/a тихо перетворюється на покарання.
NA_MARKER = "n/a"


class RubricDrift(RuntimeError):
    """Версія рубрики розійшлась з її вмістом — оцінки не порівнювані."""


@functools.cache
def load() -> dict:
    return tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))


@functools.cache
def prompts_digest() -> str:
    """Digest усіх промптів разом: імена + вміст, у стабільному порядку.

    Ім'я файла входить у digest навмисно — перейменування виміру теж зсуває рубрику.
    """
    digest = hashlib.sha256()
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@functools.cache
def dataset_digest() -> str:
    """Digest самого датасету.

    Збігу СПИСКУ id недостатньо, і це не теорія: cap-02 лишився cap-02, але його
    фікстури переписали (RPS 88->90 замінили на сплеск утричі), бо стара редакція
    карала агента за правильну відповідь. Прогони до і після цієї правки міряють різні
    речі під тими самими іменами — рівно та помилка, від якої стережуть prompts_sha і
    пін судді, тільки з іншого боку.
    """
    from evals.cases import CASES_FILE

    return hashlib.sha256(CASES_FILE.read_bytes()).hexdigest()


def rubric_version() -> str:
    return load()["judge"]["rubric_version"]


def judge_model() -> str:
    """Пін судді. Перекривається лише свідомо, змінною середовища для свіпа."""
    return os.getenv("SRE_JUDGE_MODEL") or load()["judge"]["model"]


def check_rubric_integrity() -> None:
    """Два гарди, що ловлять протилежні помилки. Обидва потрібні.

    prompts_sha         — промпт правили, а версію не бампнули;
    rubric-history.json — версію лишили стару, а digest під нею перезаписали.
    Перший сам по собі пропускає другу помилку: досить перезаписати sha, не чіпаючи
    версію, і дві різні рубрики поїдуть під одним ярликом.
    """
    actual, declared = prompts_digest(), load()["judge"]["prompts_sha"]
    if actual != declared:
        raise RubricDrift(
            f"промпти змінились, а rubric_version лишилась {rubric_version()!r}.\n"
            f"  очікувано: {declared}\n  фактично:  {actual}\n"
            f"Бампни rubric_version і prompts_sha (make eval-rubric-bump) — "
            f"інакше нові оцінки будуть несумісні зі старими під тим самим ярликом.")

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    recorded = history.get(rubric_version())
    if recorded is None:
        raise RubricDrift(f"версії {rubric_version()!r} немає в rubric-history.json")
    if recorded != actual:
        raise RubricDrift(
            f"версія {rubric_version()!r} вже позначала іншу рубрику ({recorded[:12]}…). "
            f"Перевикористання ярлика зводить дві різні рубрики в одну — бампни версію.")


def dimensions() -> dict[str, dict]:
    return load()["dimensions"]


def system_prompt() -> str:
    return (PROMPTS_DIR / SYSTEM_PROMPT_FILE).read_text(encoding="utf-8").strip()


def dimension_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def applicable(name: str, case: dict) -> bool:
    """Чи має вимір знаменник на цьому кейсі.

    Незастосовність оголошена в конфізі, тому це СТРУКТУРНИЙ n/a: виклик судді на нього
    не витрачається взагалі. Не плутати з чесним n/a від судді — той означає «знаменник
    мав би бути, але в артефактах його немає».
    """
    spec = dimensions()[name]
    if case.get("kind") not in spec.get("kinds", []):
        return False
    if any(not case.get(flag) for flag in spec.get("requires", [])):
        return False
    return not any(case.get(flag) for flag in spec.get("excludes", []))


def gate() -> dict:
    return load()["gate"]


def dataset_rules() -> dict:
    return load()["dataset"]
