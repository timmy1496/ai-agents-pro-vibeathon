"""Ексклюзивний доступ до пам'яті одного треда.

Вебхук Alertmanager і слухач Slack — різні процеси, які пишуть в один thread_id.
LangGraph складає чекпоінти ланцюжком за parent_checkpoint_id: прогін, що стартував
раніше, дописує результат до СВОГО батька, який чужих змін ще не бачив, і мовчки їх
затирає. Так під час демо зник звіт RCA — жарт, замовлений посеред розслідування,
відкотив пам'ять треда на стан до публікації, і наступне «а чи було таке раніше?»
прийшло в контекст із самих жартів.

SQLite тут не рятує: блокування БД відпрацювали, конфлікт логічний, не на рівні файлу.
Тому робота з тредом серіалізується — flock, бо процеси різні.
"""
from __future__ import annotations

import contextlib
import fcntl
import re
import threading

from agents.config import DATA_DIR

LOCK_DIR = DATA_DIR / "locks"
_held = threading.local()


@contextlib.contextmanager
def thread_lock(thread_id: str):
    """Тримає тред за собою на час прогону графа або запису стану.

    Реентрантний у межах потоку: розслідування тримає лок і всередині дописує
    пам'ять — без цього flock на другому дескрипторі заблокував би сам себе.
    """
    mine = getattr(_held, "ids", None)
    if mine is None:
        mine = _held.ids = set()
    if thread_id in mine:
        yield
        return

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{re.sub(r'[^A-Za-z0-9._-]', '_', thread_id)[:120]}.lock"
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        mine.add(thread_id)
        try:
            yield
        finally:
            mine.discard(thread_id)
            fcntl.flock(handle, fcntl.LOCK_UN)
