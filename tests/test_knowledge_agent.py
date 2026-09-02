"""Димовий тест A1: граф збирається, тул викликається, відповідь доходить до стану.

Модель підставна — без API-ключа і без мережі.
"""
import pytest
from langchain_core.messages import AIMessage

from tests.fake_model import ScriptedChatModel


@pytest.fixture(scope="module", autouse=True)
def kb_in_memory():
    from agents.kb import store

    store.QDRANT_URL = ":memory:"
    store.client.cache_clear()
    store.reindex()


def build(script: list[AIMessage]):
    from agents.knowledge_agent import build_agent

    model = ScriptedChatModel(script=script, calls=[])
    return build_agent(model=model), model


def test_agent_binds_all_kb_tools():
    from agents.knowledge_agent import TOOLS

    agent, model = build([AIMessage(content="ок")])
    agent.invoke({"messages": [{"role": "user", "content": "привіт"}]})
    assert {t.name for t in model.bound_tools} == {t.name for t in TOOLS}


def test_agent_executes_tool_call_and_gets_kb_result():
    """Агент просить search_kb -> граф справді виконує пошук -> результат у стані."""
    agent, _ = build([
        AIMessage(content="", tool_calls=[{
            "name": "search_kb", "id": "call_1",
            "args": {"query": "NullPointer on payment_ref", "limit": 3},
        }]),
        AIMessage(content="Це регресія релізу [kb/postmortems/2025-10-21-...md]"),
    ])
    result = agent.invoke({"messages": [{"role": "user", "content": "що це за помилка?"}]})

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert len(tool_messages) == 1, "тул мав виконатись рівно раз"
    assert "2025-10-21" in tool_messages[0].content, "пошук мав знайти постмортем про реліз"
    assert result["messages"][-1].content.startswith("Це регресія")


def test_get_service_tool_flows_through_agent():
    agent, _ = build([
        AIMessage(content="", tool_calls=[{
            "name": "get_service", "id": "call_1", "args": {"name": "demo-chaos-svc"},
        }]),
        AIMessage(content="tier 1, власник team-orders"),
    ])
    result = agent.invoke({"messages": [{"role": "user", "content": "хто власник demo-chaos-svc?"}]})

    tool_output = [m for m in result["messages"] if m.type == "tool"][0].content
    assert "team-orders" in tool_output and "orders-db" in tool_output


def test_system_prompt_forbids_inventing():
    """Правило fail-closed має бути в промпті — на ньому тримається groundedness."""
    from agents.knowledge_agent import SYSTEM_PROMPT

    assert "у базі немає" in SYSTEM_PROMPT
