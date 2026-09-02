"""Розбиття markdown-документів KB на чанки по H2 з успадкуванням frontmatter.

Чому по H2: постмортеми й runbooks написані за єдиним шаблоном, де секція = смислова
одиниця ("Root cause", "Timeline", "Дії"). Далі секції жадібно пакуються до ~300 токенів,
щоб коротка "Summary" не їхала в індекс окремим недочанком, а довгий "Timeline" не різався
посеред думки.
"""
from __future__ import annotations

import pathlib
import re

import yaml

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Українську пишуть чотирма різними апострофами. Для BM25 це чотири різні токени,
# тому "пам'яті" з документа не знаходиться по "памʼяті" із запиту. Нормалізуємо
# на індексації і на пошуку — інакше sparse-гілка гібрида просто мовчить.
APOSTROPHES = str.maketrans({"\u02bc": "'", "\u2019": "'", "\u0060": "'", "\u00b4": "'"})
H1 = re.compile(r"^# +(.+)$", re.MULTILINE)
# ~300 токенів української ≈ 900 символів. Секцію більшу за ліміт не ріжемо.
TARGET_CHARS = 900


def normalize(text: str) -> str:
    """Зводить варіанти апострофа до одного — див. APOSTROPHES."""
    return text.translate(APOSTROPHES)


def parse(text: str) -> tuple[dict, str]:
    """Відділяє YAML-frontmatter від тіла документа."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end():]


def split_sections(body: str) -> list[tuple[str, str]]:
    """Ділить тіло на (заголовок H2, текст). Преамбула до першого H2 йде із заголовком ''."""
    parts = re.split(r"^## +(.+)$", body, flags=re.MULTILINE)
    sections = [("", parts[0])]
    sections += [(h.strip(), c) for h, c in zip(parts[1::2], parts[2::2])]
    return [(h, c) for h, c in sections if c.strip()]


def pack_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Жадібно склеює сусідні секції, поки чанк не дотягне до TARGET_CHARS."""
    packed: list[tuple[list[str], list[str]]] = []
    for heading, content in sections:
        piece = f"## {heading}\n{content.strip()}" if heading else content.strip()
        if packed and sum(map(len, packed[-1][1])) + len(piece) <= TARGET_CHARS:
            packed[-1][0].append(heading)
            packed[-1][1].append(piece)
        else:
            packed.append(([heading], [piece]))
    return [(headings, "\n\n".join(pieces)) for headings, pieces in packed]


def chunk_file(path: pathlib.Path, root: pathlib.Path) -> list[dict]:
    """Один markdown-файл → список чанків з метаданими для фільтрів у Qdrant."""
    meta, body = parse(normalize(path.read_text(encoding="utf-8")))
    h1 = H1.search(body)
    title = meta.get("title") or (h1.group(1).strip() if h1 else path.stem)
    body = H1.sub("", body, count=1)  # H1 несемо в title, у тексті він був би дублем

    return [
        {
            # Заголовок документа в кожному чанку — щоб секція "Root cause" не втратила,
            # чия вона: без цього ретривер плутає постмортеми між собою.
            "text": f"# {title}\n\n{content}".strip(),
            "source": str(path.relative_to(root)),
            "title": title,
            "headings": [h for h in headings if h],
            "chunk_index": index,
            "type": meta.get("type", "doc"),
            "service": meta.get("service") or (meta.get("services") or [None])[0],
            "services": meta.get("services") or ([meta["service"]] if meta.get("service") else []),
            "date": str(meta.get("date", "")),
            "tags": meta.get("tags", []),
            "root_cause_label": meta.get("root_cause_label", ""),
            "severity": meta.get("severity", ""),
        }
        for index, (headings, content) in enumerate(pack_sections(split_sections(body)))
    ]


def chunk_dir(kb_dir: pathlib.Path, root: pathlib.Path) -> list[dict]:
    return [c for path in sorted(kb_dir.rglob("*.md")) for c in chunk_file(path, root)]
