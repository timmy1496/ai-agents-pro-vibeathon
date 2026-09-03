"""PreToolUse-хук: деструктив відсікається ДО людини і до вузла тулів.

Ці тести навмисно ганяють ГРАФ, а не `propose_action.invoke()` напряму. Стара версія
перевіряла тул у обхід middleware, тому назва тесту («never reach a human») стверджувала
більше, ніж тест перевіряв: у графі HITL зупиняв виконання раніше, ніж тіло тула з
регексом взагалі запускалось, і людина бачила `kubectl delete` першою.
"""
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests import fixtures
from tests.fake_model import ScriptedChatModel

DESTRUCTIVE_COMMANDS = [
    "kubectl delete pod chaos-svc-abc",
    "kubectl delete deploy/x --force --grace-period=0",
    "DROP TABLE orders;",
    "helm uninstall payment-gateway",
    "rm -rf /var/lib/data",
    "DELETE FROM users WHERE 1=1",
    "terraform destroy",
]


@pytest.fixture(autouse=True)
def stubbed_backends(monkeypatch, tmp_path):
    import agents.tools.actions as actions
    import agents.tools.stand as stand
    from agents.tools import observability

    def fake_get(url, params):
        if "/loki/" in url:
            return fixtures.loki_lines(fixtures.ERROR_LOG_LINES)
        return fixtures.prom_range([0.01, 0.34], service="demo-chaos-svc")

    monkeypatch.setattr(observability, "_get", fake_get)
    monkeypatch.setattr(stand, "DATA_DIR", tmp_path)
    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")


def propose(command: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "propose_action", "id": "p1",
        "args": {"service": "demo-chaos-svc", "action": "прибрати под",
                 "reason": "здається, так швидше", "command": command},
    }])


def run(command: str) -> dict:
    from agents.incident_agent import build_agent

    agent = build_agent(model=ScriptedChatModel(script=[propose(command), AIMessage("готово")],
                                                calls=[]),
                        checkpointer=InMemorySaver())
    return agent.invoke({"messages": [{"role": "user", "content": "алерт"}]},
                        config={"configurable": {"thread_id": "guard"}})


@pytest.mark.parametrize("command", DESTRUCTIVE_COMMANDS)
def test_destructive_proposal_is_blocked_before_the_human_sees_it(command):
    """Ключове: НЕМАЄ interrupt. Людину навіть не питали."""
    result = run(command)

    assert "__interrupt__" not in result, (
        f"{command}: граф зупинився на підтвердженні — тобто пропозицію показали людині "
        f"раніше, ніж відкинула політика")
    blocked = [m for m in result["messages"] if m.type == "tool" and m.name == "propose_action"]
    assert blocked, "модель мала отримати відповідь про блокування, а не тишу"
    assert "ЗАБЛОКОВАНО ПОЛІТИКОЮ" in blocked[0].content
    assert blocked[0].status == "error"


@pytest.mark.parametrize("command", DESTRUCTIVE_COMMANDS)
def test_destructive_command_never_executes_the_tool(command):
    """Блокування — це не «виконали і повідомили»: тіло тула не запускається взагалі."""
    result = run(command)

    outputs = [m.content for m in result["messages"]
               if m.type == "tool" and m.name == "propose_action"]
    assert not any("awaiting_human_approval" in o for o in outputs), \
        "тул відпрацював попри блокування"


@pytest.mark.parametrize("command", [
    "kubectl rollout undo deploy/demo-chaos-svc",
    "kubectl scale deploy/notification-worker --replicas=20",
    "helm upgrade demo-chaos-svc ./chart --set image.tag=1.4.2",
])
def test_safe_proposal_still_stops_for_a_human(command):
    """Guard не має з'їсти HITL: недеструктивна дія так само чекає «ок»."""
    result = run(command)

    assert "__interrupt__" in result, f"{command}: пропозиція мала дочекатись людини"
    assert not [m for m in result.get("messages", []) if m.type == "tool"], \
        "жоден write-тул не мав виконатись до підтвердження"


def test_destructive_text_in_the_action_field_is_caught_too():
    """Команду можна сховати в опис дії — фільтр дивиться на обидва поля."""
    from agents.guardrails import is_destructive

    assert is_destructive({"action": "просто зроби kubectl delete pod x", "command": ""})
    assert not is_destructive({"action": "відкотити реліз", "command": ""})


def test_guard_runs_before_the_human_in_the_loop_middleware():
    """Пін на порядок: LangChain зшиває after_model З КІНЦЯ списку, тому guard — останній.

    Тест виглядає дріб'язковим, але саме ця деталь і була дефектом: переставити два
    рядки місцями означає повернути стару поведінку, і жоден інший тест цього не побачить,
    бо `when` у HITL прикриває найгірший випадок мовчки.
    """
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    from agents.guardrails import DestructiveActionGuard, approval_middleware

    chain = approval_middleware()
    assert isinstance(chain[-1], DestructiveActionGuard), \
        "guard має стояти ОСТАННІМ у списку, щоб виконатись ПЕРШИМ"
    assert any(isinstance(m, HumanInTheLoopMiddleware) for m in chain)
