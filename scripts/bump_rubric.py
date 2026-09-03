"""Бамп версії рубрики після правки evals/prompts/*.md.

Правка промпта судді зсуває еталон так само, як тиха заміна моделі судді: завтрашня
«регресія агента» може виявитись рухом власної лінійки. Тому кожна правка мусить
отримати нову версію, і два гарди це стережуть (див. evals/config.py).

Робити це руками — тричі переписати ту саму суму в двох файлах, тож цей скрипт існує
рівно для того, щоб гард не хотілося обійти.

    python -m scripts.bump_rubric            # r3 -> r4
    python -m scripts.bump_rubric --check    # нічого не пише, лише каже, чи потрібен бамп
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from evals import config


def next_version(current: str) -> str:
    match = re.fullmatch(r"r(\d+)", current)
    if not match:
        raise SystemExit(f"незрозуміла версія рубрики {current!r}: чекав формат rN")
    return f"r{int(match.group(1)) + 1}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="лише перевірити, нічого не писати")
    args = parser.parse_args(argv)

    digest, declared = config.prompts_digest(), config.load()["judge"]["prompts_sha"]
    current = config.rubric_version()

    if digest == declared:
        print(f"рубрика {current} збігається з промптами — бампати нічого")
        return 0
    if args.check:
        print(f"промпти змінились: {current} більше не описує prompts/ "
              f"({declared[:12]}… -> {digest[:12]}…)\nЗапусти: make eval-rubric-bump",
              file=sys.stderr)
        return 1

    version = next_version(current)
    text = config.CONFIG_FILE.read_text(encoding="utf-8")
    text = text.replace(f'rubric_version = "{current}"', f'rubric_version = "{version}"')
    text = text.replace(f'prompts_sha = "{declared}"', f'prompts_sha = "{digest}"')
    config.CONFIG_FILE.write_text(text, encoding="utf-8")

    history = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
    history[version] = digest  # старі записи не чіпаємо: історія — це те, що вже виміряно
    config.HISTORY_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"рубрика {current} -> {version} ({digest[:12]}…)\n"
          f"Оцінки під {version} НЕ порівнюються з оцінками під {current}: "
          f"зсунувся сам еталон, а не якість агента.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
