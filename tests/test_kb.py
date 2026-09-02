"""Перевірка шару знань A1: чанкінг і те, що гібридний пошук справді влучає.

Пошук ганяємо на Qdrant у режимі ":memory:" — без docker, тому це придатне для CI-гейта.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# --- чанкінг -----------------------------------------------------------------

def test_frontmatter_and_h2_split():
    from agents.kb.chunker import chunk_file

    chunks = chunk_file(ROOT / "kb/postmortems/2025-10-21-demo-chaos-svc-release-5xx.md", ROOT)
    assert len(chunks) >= 2, "постмортем має розбитись мінімум на кілька чанків"
    assert all(c["service"] == "demo-chaos-svc" for c in chunks), "frontmatter не успадкувався"
    assert all(c["root_cause_label"] == "release" for c in chunks)
    assert any("Root cause" in c["headings"] for c in chunks), "секції мають лишатись у метаданих"
    assert all(c["text"].startswith("# ") for c in chunks), "чанк має починатись заголовком документа"
    assert all(c["text"].count(c["title"]) == 1 for c in chunks), "заголовок не має дублюватись"


def test_short_sections_are_packed_not_left_alone():
    from agents.kb.chunker import TARGET_CHARS, pack_sections, split_sections

    body = "## A\nкоротко\n\n## B\nтеж коротко\n\n## C\n" + "довгий текст. " * 200
    packed = pack_sections(split_sections(body))
    assert packed[0][0] == ["A", "B"], "короткі секції мали склеїтись в один чанк"
    assert len(packed) == 2, "секція, більша за ліміт, лишається окремим чанком"
    assert len(packed[1][1]) > TARGET_CHARS, "довгу секцію не ріжемо посеред думки"


def test_whole_kb_chunks():
    from agents.kb.chunker import chunk_dir

    chunks = chunk_dir(ROOT / "kb", ROOT)
    assert len(chunks) > 30, f"очікували десятки чанків, отримали {len(chunks)}"
    assert {c["type"] for c in chunks} == {"postmortem", "runbook", "tech"}


# --- пошук --------------------------------------------------------------------

@pytest.fixture(scope="module")
def indexed(monkeypatch_module=None):
    import agents.config as config
    from agents.kb import store

    config.QDRANT_URL = ":memory:"
    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    store.reindex()
    return store


@pytest.mark.parametrize("query, expected_source", [
    # BM25 має влучити по точному ідентифікатору з логів
    ("NullPointer on payment_ref", "kb/postmortems/2025-10-21-demo-chaos-svc-release-5xx.md"),
    ("dial tcp orders-db:5432 i/o timeout", "kb/postmortems/2025-08-30-demo-chaos-svc-orders-db-timeout.md"),
])
def test_search_finds_expected_document(indexed, query, expected_source):
    sources = [hit["source"] for hit in indexed.search(query, limit=3)]
    assert expected_source in sources, f"{query!r} -> {sources}"


def test_apostrophe_variants_give_same_results(indexed):
    """U+02BC і U+0027 — той самий апостроф для людини і різні токени для BM25."""
    modifier = [h["source"] for h in indexed.search("нестача памʼяті і рестарти", limit=5)]
    ascii_ = [h["source"] for h in indexed.search("нестача пам'яті і рестарти", limit=5)]
    assert modifier == ascii_ and modifier, "нормалізація апострофа не працює"


@pytest.mark.parametrize("symptoms, expected_label", [
    ("5xx зросли одразу після викату нової версії", "release"),
    ("latency вперлась у таймаут, деплою не було, у логах i/o timeout до бази", "dependency"),
    ("памʼять росте пилкою, контейнер вбиває OOMKilled", "resources"),
])
def test_similar_incidents_hit_right_root_cause_class(indexed, symptoms, expected_label):
    """Головне, що A2 бере з KB — клас причини, а не конкретний файл.

    Кілька постмортемів на тему релевантні одночасно, тому перевіряємо не точний
    документ, а що потрібний root_cause_label є серед топ-3.
    """
    labels = [h["root_cause_label"] for h in indexed.search(symptoms, limit=3, type="postmortem")]
    assert expected_label in labels, f"{symptoms!r} -> {labels}"


@pytest.mark.parametrize("query, expected_source", [
    ("сервіс рестартує через нестачу памʼяті, що робити", "kb/runbooks/oomkilled-restarts.md"),
    ("як діяти при сплеску помилок 5xx", "kb/runbooks/high-error-rate.md"),
    ("недоступна база даних, таймаути зʼєднання", "kb/runbooks/dependency-down.md"),
])
def test_runbook_lookup_needs_type_filter(indexed, query, expected_source):
    """Питання "що робити" йдуть із doc_type='runbook' — постмортемів у корпусі вдвічі
    більше, і без фільтра вони перебивають runbook."""
    sources = [hit["source"] for hit in indexed.search(query, limit=3, type="runbook")]
    assert expected_source in sources, f"{query!r} -> {sources}"


def test_filter_by_type_and_service(indexed):
    hits = indexed.search("памʼять рестарти", limit=5, type="postmortem")
    assert hits and all(h["type"] == "postmortem" for h in hits)

    hits = indexed.search("деплой", limit=5, service="checkout-api")
    assert hits and all(h["service"] == "checkout-api" for h in hits)


def test_fail_closed_on_irrelevant_query(indexed):
    """Порожньо краще за вигадку: висока планка відсіює шум."""
    assert indexed.search("рецепт борщу з пампушками", limit=3, min_score=0.9) == []


# --- каталог -------------------------------------------------------------------

def test_catalog_tools():
    from agents.tools.catalog import get_service, list_services

    svc = get_service.invoke({"name": "demo-chaos-svc"})
    assert svc["tier"] == 1 and "orders-db" in svc["deps"] and svc["runbook"]

    assert get_service.invoke({"name": "no-such-service"})["error"]
    assert all(s["tier"] == 1 for s in list_services.invoke({"tier": 1}))
