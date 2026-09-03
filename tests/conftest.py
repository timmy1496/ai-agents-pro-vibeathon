"""Ізоляція тестів від зовнішнього світу.

`_kb_offline` — autouse і сесійна навмисно. Без неї `store.QDRANT_URL` лишався
бойовим `http://localhost:6333`, і файли, що ходять у KB (A2 прогріває його на старті
через `ensure_indexed`), проходили ЛИШЕ тому, що `tests/test_eval_gate.py` сортується
раніше і його фікстура `kb_indexed` глобально перемикала модуль у `:memory:`. Запуск
одного файла окремо (`pytest tests/test_incident_agent.py`) падав 6 з 7 на
`Connection refused` — і це були саме ті тести, що стережуть guardrails: PII,
Context-Minimization, стелю кроків, HITL.

Перемикання і переіндексація свідомо розділені: перше коштує нуль і потрібне всім,
друге тягне ONNX-модель і потрібне лише тестам, що справді шукають по KB.
"""
import pathlib

import pytest

# Підлога на кількість зібраних тестів. Єдине, чого pytest не покриває сам: файл, який
# випав зі збору (перейменування, помилка імпорту в conftest, зламаний маркер), тихо
# перестає перевірятись — а сьют лишається зеленим і навіть швидшає. Число піднімають
# разом з новими тестами; воно завжди трохи нижче за фактичне, щоб не червоніти на
# кожному видаленому параметрі.
MIN_COLLECTED = 240


def pytest_collection_modifyitems(session, config, items):
    """Гард працює лише на повному прогоні: `pytest tests/test_kb.py` збирає менше і це нормально."""
    targets = [a for a in config.args if not a.startswith("-")]
    full_run = not targets or all(pathlib.Path(t).name in ("tests", "") for t in targets)
    if full_run and len(items) < MIN_COLLECTED:
        raise pytest.UsageError(
            f"зібрано лише {len(items)} тестів при підлозі {MIN_COLLECTED} — "
            f"схоже, файл випав зі збору. Якщо тести свідомо видалені, опусти "
            f"MIN_COLLECTED у tests/conftest.py тим самим комітом.")


@pytest.fixture(scope="session", autouse=True)
def _kb_offline():
    """Qdrant у пам'яті для всієї сесії: жоден тест не ходить у мережу за індексом."""
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    yield store
    store.client.cache_clear()


@pytest.fixture(scope="session")
def kb_indexed(_kb_offline):
    """Один індекс KB на всю сесію — переіндексація коштує секунди, не мілісекунди."""
    _kb_offline.reindex()
    return _kb_offline


@pytest.fixture(scope="session", autouse=True)
def _no_tracing():
    """Трейси в тестах не потрібні, а експортер даремно стукає в :3001 і ретраїть.

    Вимикаємо прапорцем, а не підміною самої функції: test_tracing.py перевіряє
    поведінку `_handler` і йому потрібен справжній кешований об'єкт.
    """
    from agents import observability

    observability.LANGFUSE_ENABLED = False
    observability._handler.cache_clear()
    yield
    observability._handler.cache_clear()
