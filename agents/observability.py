"""Трейси в Langfuse. Вимикається однією змінною і мовчки деградує, якщо сервера немає.

Спостережуваність не має права ламати інцидент: якщо Langfuse не піднятий, агент
працює далі без трейсів, а не падає. Тому все загорнуте в try і кешоване.
"""
from __future__ import annotations

import functools
import logging

from agents.config import (
    LANGFUSE_ENABLED, LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
)

log = logging.getLogger(__name__)


@functools.cache
def _handler():
    if not LANGFUSE_ENABLED:
        return None
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        Langfuse(public_key=LANGFUSE_PUBLIC_KEY, secret_key=LANGFUSE_SECRET_KEY,
                 host=LANGFUSE_HOST)
        return CallbackHandler()
    except Exception as error:  # сервер не піднятий, старий SDK, немає ключів
        log.warning("Langfuse вимкнено: %s", error)
        return None


def trace_config(thread_id: str, tags: list[str] | None = None, **metadata) -> dict:
    """Конфіг для .invoke(): трейс прив'язаний до треда, тому інцидент видно цілим деревом.

    session_id = thread_id дає в Langfuse одну сесію на інцидент — з вартістю і токенами
    по всіх кроках supervisor -> agent -> tool, а не по окремих викликах.
    """
    config: dict = {"configurable": {"thread_id": thread_id},
                    "metadata": {"langfuse_session_id": thread_id,
                                 "langfuse_tags": tags or [], **metadata}}
    handler = _handler()
    if handler is not None:
        config["callbacks"] = [handler]
    return config
