"""Інструменти пошуку по базі знань (постмортеми, runbooks, tech-нотатки)."""
from __future__ import annotations

from langchain_core.tools import tool

from agents.kb import store

SNIPPET_CHARS = 1200  # цілої секції вистачає для відповіді; довші ріжемо, щоб не роздувати контекст


def _brief(hit: dict) -> dict:
    return {
        "source": hit["source"],
        "title": hit["title"],
        "sections": hit["headings"],
        "type": hit["type"],
        "service": hit["service"],
        "date": hit["date"],
        "root_cause_label": hit["root_cause_label"],
        "score": hit["score"],
        "text": hit["text"][:SNIPPET_CHARS],
    }


@tool
def search_kb(query: str, service: str | None = None,
              doc_type: str | None = None, limit: int = 5) -> list[dict]:
    """Пошук по базі знань компанії: постмортеми, runbooks, tech-нотатки.

    doc_type звужує до 'postmortem' | 'runbook' | 'tech'. Порожній результат означає,
    що в базі немає відповіді — так і кажи, не додумуй.
    """
    hits = store.search(query, limit=limit, service=service, type=doc_type)
    return [_brief(h) for h in hits]


@tool
def similar_incidents(symptoms: str, service: str | None = None, limit: int = 3) -> list[dict]:
    """Схожі минулі інциденти за описом симптомів — тільки постмортеми.

    Передавай симптоми, а не назву алерту: "5xx зросли одразу після деплою, у логах
    NullPointer" знайде більше, ніж "HighErrorRate".
    """
    hits = store.search(symptoms, limit=limit, service=service, type="postmortem")
    return [_brief(h) for h in hits]
