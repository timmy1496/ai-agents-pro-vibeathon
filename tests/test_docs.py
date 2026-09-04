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
