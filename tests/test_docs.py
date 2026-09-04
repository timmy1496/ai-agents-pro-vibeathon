"""Документація теж може брехати, і бреше вона тихо.

Числа в README і DEMO.md розходились з реальністю тричі поспіль — не через недбалість,
а тому що оновлювати їх не було чиїм завданням. Тут воно стає завданням того, хто
змінює предмет: додав тестів — поправ число, інакше сьют червоний.

Перевіряється лише те, що є ФАКТОМ про репозиторій. Формулювання, оцінки і тон —
не справа тестів.
"""
import pathlib
import re

import pytest

import tests.conftest as conf
from evals import cases, config

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = {name: (ROOT / name) for name in ("README.md", "DEMO.md", "docs/EVALS.md")}


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {name: path.read_text(encoding="utf-8") for name, path in DOCS.items()}


def test_every_referenced_doc_exists():
    for name, path in DOCS.items():
        assert path.exists(), f"{name} згадується в наборі, але його немає"


def test_quoted_test_count_matches_reality(texts):
    """«N тестів» у доках проти того, що pytest справді зібрав."""
    if not conf.COLLECTED:
        pytest.skip("часткий прогін збирає менше — порівнювати нема з чим")

    for name, text in texts.items():
        for quoted in re.findall(r"\b(\d{3}) тест", text):
            assert int(quoted) == conf.COLLECTED, (
                f"{name} каже «{quoted} тестів», а зібралось {conf.COLLECTED}. "
                f"Постав актуальне число тим самим комітом.")


def test_quoted_dataset_shape_matches_the_dataset(texts):
    """Розмір датасету в README проти самого cases.yaml."""
    readme = texts["README.md"]
    counts = {kind: len(cases.by_kind(kind)) for kind in ("rca", "kb", "policy")}

    assert f"{len(cases.load())} кейси" in readme or f"{len(cases.load())} кейсів" in readme
    assert f"{counts['rca']} RCA" in readme
    assert f"{counts['policy']} на політику" in readme


def test_documented_dimensions_are_the_real_ones(texts):
    """Вимір, доданий у eval.toml, не має лишитись неописаним."""
    evals_md = texts["docs/EVALS.md"]
    for dimension in config.dimensions():
        assert f"`{dimension}`" in evals_md, f"вимір {dimension} не описаний у docs/EVALS.md"


def test_links_between_docs_are_not_broken(texts):
    """Відносні посилання в md ведуть у файли, які існують."""
    for name, text in texts.items():
        base = (ROOT / name).parent
        for target in re.findall(r"\]\((?!https?:|#)([^)]+)\)", text):
            path = (base / target.split("#")[0]).resolve()
            assert path.exists(), f"{name}: посилання на {target} нікуди не веде"


def test_adrs_follow_the_madr_conventions():
    """MADR: NNNN-slug.md, front-matter зі статусом і датою, заголовок і три секції.

    Без цього ADR перетворюються на вільні нотатки: перший же запис без «Considered
    Options» — це рішення без альтернатив, тобто не рішення, а констатація.
    """
    adr_dir = ROOT / "docs" / "adr"
    files = sorted(adr_dir.glob("*.md"))
    assert files, "тека ADR порожня"

    seen_indexes = set()
    for path in files:
        assert re.fullmatch(r"\d{4}-[a-z0-9-]+\.md", path.name), \
            f"{path.name}: очікується NNNN-slug-with-dashes.md"
        index = path.name[:4]
        assert index not in seen_indexes, f"дубльований індекс {index}"
        seen_indexes.add(index)

        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{path.name}: немає front-matter"
        head = text.split("---", 2)[1]
        assert re.search(r"^status: ", head, re.M), f"{path.name}: немає status"
        assert re.search(r"^date: \d{4}-\d{2}-\d{2}", head, re.M), f"{path.name}: немає дати"

        body = text.split("---", 2)[2]
        assert re.search(r"^# .+", body, re.M), f"{path.name}: немає заголовка"
        for section in ("## Context and Problem Statement", "## Considered Options",
                        "## Decision Outcome"):
            assert section in body, f"{path.name}: немає секції {section!r}"
        assert body.count("\n* ") >= 2 or body.count("\n- ") >= 2, \
            f"{path.name}: менше двох розглянутих варіантів — це не рішення"
        assert "{" not in body, f"{path.name}: незаповнений плейсхолдер шаблону"


def test_no_registry_file_shadows_the_directory_listing():
    """Конвенція: індекс ADR — це `ls`, а не окремий файл, який розходиться з реальністю."""
    for name in ("index.md", "README.md", "INDEX.md", "toc.md"):
        assert not (ROOT / "docs" / "adr" / name).exists(), \
            f"docs/adr/{name}: реєстр не ведеться, лістинг теки і є індексом"
