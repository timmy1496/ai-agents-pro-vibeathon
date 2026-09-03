"""Спільна пам'ять тредів.

Вебхук Alertmanager і слухач Slack — різні процеси, і в кожного був власний
InMemorySaver. Через це згадка в треді інциденту не бачила самого розслідування:
питання «а чи було таке раніше?» приходило в порожній контекст, і агент чесно
відповідав, що не розуміє, про що йдеться.

SQLite на диску вирішує обидва боки: стан спільний між процесами і переживає рестарт.
"""
from __future__ import annotations

import functools
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from agents.config import DATA_DIR

CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite"


@functools.cache
def saver() -> SqliteSaver:
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: агент виконує тули в кількох потоках
    connection = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    return SqliteSaver(connection)
