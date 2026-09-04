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
import os
import pathlib
import sys

import pytest

# Підлога на кількість зібраних тестів. Єдине, чого pytest не покриває сам: файл, який
# випав зі збору (перейменування, помилка імпорту в conftest, зламаний маркер), тихо
# перестає перевірятись — а сьют лишається зеленим і навіть швидшає. Число піднімають
# разом з новими тестами; воно завжди трохи нижче за фактичне, щоб не червоніти на
# кожному видаленому параметрі.
MIN_COLLECTED = 335

_EXIT_STATUS: list[int] = []


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Вийти з кодом, який pytest уже вирішив, не чекаючи згортання інтерпретатора.

    Локальний Qdrant тримає вбудований сегмент у C++, і його деструктори на виході
    процесу вступають у гонку з потоками ONNX: приблизно раз на три прогони процес
    падає з SIGABRT (`recursive_mutex lock failed`) уже ПІСЛЯ останнього тесту. Сьют
    зелений, код виходу 134 — тобто CI червоніє на повністю зеленому прогоні, і робить
    це плаваюче, що гірше за стабільну поломку.

    Явне закриття клієнта проблему не знімає (перевірено), бо клієнтів за сесію
    створюється кілька. А вердикт уже винесено: все, що відбувається після цього рядка,
    не може його змінити — деструктори вбудованої бази не несуть інформації про тести.

    Вихід відкладений до pytest_unconfigure: у sessionfinish підсумковий рядок
    («336 passed») ще не надрукований, і ранній вихід його з'їдав — тобто лікування
    коштувало б звіту, заради якого сьют і запускають.

    Знімати цей хук можна тоді, коли Qdrant перестане бути in-process.
    """
    del session
    _EXIT_STATUS.append(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Тут увесь вивід уже написаний, а деструктори ще не запускались."""
    del config
    if not _EXIT_STATUS:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_EXIT_STATUS[-1])


def pytest_collection_modifyitems(session, config, items):
    """Гард працює лише на повному прогоні: `pytest tests/test_kb.py` збирає менше і це нормально."""
    global COLLECTED
    targets = [a for a in config.args if not a.startswith("-")]
    full_run = not targets or all(pathlib.Path(t).name in ("tests", "") for t in targets)
    COLLECTED = len(items) if full_run else 0
    if full_run and len(items) < MIN_COLLECTED:
        raise pytest.UsageError(
            f"зібрано лише {len(items)} тестів при підлозі {MIN_COLLECTED} — "
            f"схоже, файл випав зі збору. Якщо тести свідомо видалені, опусти "
            f"MIN_COLLECTED у tests/conftest.py тим самим комітом.")


@pytest.fixture(autouse=True)
def no_real_slack(monkeypatch):
    """Жоден тест не має права написати в реальний Slack.

    Транспорт вмикається наявністю SLACK_BOT_TOKEN у .env — тобто варто розробнику
    додати токен, як прогін тестів починає слати повідомлення в робочий канал.
    Тести, яким потрібен Slack-шлях, підміняють _call і вмикають токен явно.
    """
    from agents.tools import slack

    monkeypatch.setattr(slack, "SLACK_BOT_TOKEN", "")


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Жоден тест не пише у справжній data/ проєкту.

    Інакше стан протікає між прогонами: тест, що дедуплікує алерти, записав свій
    fingerprint у робочий файл і на другому запуску сам себе відфільтрував.
    """
    import agents.app as app_module
    import agents.checkpoint as checkpoint
    from agents import jokes
    import agents.tools.actions as actions
    from agents.tools import slack

    # чекпойнтер теж пише на диск: без ізоляції історія тредів накопичується
    # між прогонами і тести починають бачити чужі повідомлення
    checkpoint.saver.cache_clear()
    monkeypatch.setattr(checkpoint, "CHECKPOINT_DB", tmp_path / "checkpoints.sqlite")
    # app.supervisor створюється при імпорті модуля, тому підміни шляху йому замало —
    # без перезбирання графа тести пишуть у справжню базу і бачать стан минулих прогонів
    from agents.supervisor import build_supervisor

    monkeypatch.setattr(app_module, "supervisor", build_supervisor())
    # Жарт — оздоблення, а не частина потоку інциденту: тести цього потоку не мають
    # ламатись від зміни списку жартів. test_jokes вмикає їх у себе явно.
    monkeypatch.setattr(jokes, "JOKES_ENABLED", False)
    monkeypatch.setattr(app_module, "PROCESSED", tmp_path / "processed_alerts.json")
    monkeypatch.setattr(app_module, "SLACK_FILE", tmp_path / "slack_threads.json")
    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack_threads.json")
    monkeypatch.setattr(slack, "THREAD_MAP", tmp_path / "slack_thread_map.json")


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch, request):
    """Тести не мають права звертатись до реальної моделі.

    Провайдер (API-ключ чи підписка через Claude Code CLI) — режим роботи, не режим
    тестування: прогін має лишатись безкоштовним, офлайновим і відтворюваним. Тести
    підставляють ScriptedChatModel, а resolve() віддає готовий екземпляр як є.

    Виняток — тести самого резолвера і провайдера: вони перевіряють, що клієнт
    створюється правильно, але не викликають його.
    """
    if request.node.get_closest_marker("uses_real_models") or \
            request.node.module.__name__.endswith(("test_models", "test_claude_code_provider")):
        return

    import agents.models as models

    def refuse(model, temperature=0.0):
        raise AssertionError(
            f"тест намагається створити реальну модель ({model}). "
            f"Підстав ScriptedChatModel з tests/fake_model.py")

    monkeypatch.setattr(models, "_build", refuse)


@pytest.fixture(scope="session", autouse=True)
def _kb_offline():
    """Qdrant у пам'яті для всієї сесії: жоден тест не ходить у мережу за індексом.

    Локальний QdrantClient тримає вбудований сегмент, і його деструктор на виході
    інтерпретатора вступає в гонку з потоками ONNX: процес падає з SIGABRT
    (`recursive_mutex lock failed`) уже ПІСЛЯ останнього тесту. Сьют зелений, код
    виходу 134 — тобто CI червоніє на повністю зеленому прогоні, і плаваюче.

    Тому клієнт закривається ЯВНО в кінці сесії, поки все ще живе. Кеш при цьому не
    чиститься навмисно: інакше наступне звернення створило б новий клієнт, деструктор
    якого дочекався б того самого виходу.
    """
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store._shared_client.cache_clear()
    yield store

    if store._shared_client.cache_info().currsize:
        store._shared_client().close()


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
