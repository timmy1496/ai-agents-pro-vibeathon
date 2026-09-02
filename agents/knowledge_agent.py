"""A1 · Knowledge Agent — шар знань.

Відповідає на питання по KB і працює як subagent для інших агентів: ті питають
вузьким запитом і отримують підсумок, а не сирі чанки. Модель дешева — тут немає
складного міркування, є дисципліна цитування.
"""
from __future__ import annotations

from langchain.agents import create_agent

from agents.config import CHEAP_MODEL
from agents.tools.catalog import get_service, list_services
from agents.tools.kb import search_kb, similar_incidents

SYSTEM_PROMPT = """Ти — Knowledge Agent SRE-команди. Відповідаєш на питання по внутрішній
базі знань: постмортеми, runbooks, tech-нотатки, сервіс-каталог.

Правила:
1. Кожне твердження має спиратись на вивід інструмента. Немає у виводі — не пишеш.
2. Якщо пошук повернув порожньо або нерелевантне — відповідай прямо: "у базі немає".
   Не добудовуй відповідь із загальних знань про SRE.
3. Після кожного факту став джерело у форматі [kb/postmortems/2025-10-21-...md].
4. Питання про сервіс починай з get_service — tier, deps і runbook часто вже є відповіддю.
5. Шукаючи схожі інциденти, передавай у similar_incidents симптоми, а не назву алерту.
6. Відповідай стисло: висновок, потім докази. Без переказу знайдених документів цілком.

Мова відповіді — українська."""

TOOLS = [search_kb, similar_incidents, get_service, list_services]


def build_agent(model: str = CHEAP_MODEL, **kwargs):
    """A1 як окремий граф. checkpointer/store передає викликач (supervisor)."""
    return create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT, **kwargs)


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Чи були у нас інциденти з OOMKilled і що допомогло?"
    result = build_agent().invoke({"messages": [{"role": "user", "content": question}]})
    print(result["messages"][-1].content)
