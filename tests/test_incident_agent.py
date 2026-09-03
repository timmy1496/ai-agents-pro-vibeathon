"""A2: траєкторія розслідування, петля критика і межі write-тулів.

Модель підставна — перевіряємо проводку і політику, не якість формулювань.
"""
import pytest
from langchain_core.messages import AIMessage

from tests import fixtures
from tests.fake_model import ScriptedChatModel


@pytest.fixture(autouse=True)
def stubbed_backends(monkeypatch, tmp_path):
    """Prometheus/Loki з фікстур, дані стенду — у тимчасовій теці."""
    import agents.tools.actions as actions
    import agents.tools.stand as stand
    from agents.tools import observability

    def fake_get(url, params):
        if "/loki/" in url:  # перевіряти першим: шлях Loki містить у собі шлях Prometheus
            return fixtures.loki_lines(fixtures.ERROR_LOG_LINES * 10)
        if "/api/v1/query_range" in url:
            return fixtures.prom_range([0.01, 0.34, 0.35], service="demo-chaos-svc")
        raise AssertionError(url)

    monkeypatch.setattr(observability, "_get", fake_get)
    monkeypatch.setattr(stand, "DATA_DIR", tmp_path)
    monkeypatch.setattr(actions, "SLACK_FILE", tmp_path / "slack.json")


def call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args}])


def build(script, **kwargs):
    from agents.incident_agent import build_agent

    model = ScriptedChatModel(script=script, calls=[])
    return build_agent(model=model, **kwargs), model


def test_agent_exposes_read_and_write_tools():
    from agents.incident_agent import READ_TOOLS, WRITE_TOOLS

    agent, model = build([AIMessage(content="готово")])
    agent.invoke({"messages": [{"role": "user", "content": "алерт"}]})
    bound = {t.name for t in model.bound_tools}
    assert {t.name for t in READ_TOOLS} <= bound
    assert {t.name for t in WRITE_TOOLS} <= bound


def test_golden_signals_and_patterns_run_through_agent():
    """Ключовий крок траєкторії: метрики і патерни логів реально виконуються."""
    agent, _ = build([
        call("golden_signals", {"service": "demo-chaos-svc", "minutes": 30}, "c1"),
        call("query_loki_patterns", {"service": "demo-chaos-svc"}, "c2"),
        AIMessage(content="Регресія релізу"),
    ])
    result = agent.invoke({"messages": [{"role": "user", "content": "HighErrorRate"}]})

    outputs = {m.name: m.content for m in result["messages"] if m.type == "tool"}
    assert "error_rate" in outputs["golden_signals"]
    assert "baseline_avg" in outputs["golden_signals"], "без baseline висновку не зробиш"
    assert "NullPointer on payment_ref" in outputs["query_loki_patterns"]
    assert '"total_lines": 50' in outputs["query_loki_patterns"]


def test_raw_log_lines_do_not_reach_the_model():
    """Context-Minimization: у промпт іде агрегат, а не 50 сирих рядків."""
    agent, model = build([
        call("query_loki_patterns", {"service": "demo-chaos-svc"}),
        AIMessage(content="висновок"),
    ])
    agent.invoke({"messages": [{"role": "user", "content": "алерт"}]})

    from agents.tools.observability import MAX_SAMPLES

    seen = "".join(str(m.content) for messages in model.calls for m in messages)
    # 20 входжень у логах -> сам патерн + не більше MAX_SAMPLES прикладів
    assert seen.count("NullPointer on payment_ref") <= MAX_SAMPLES + 1, \
        "у контекст мали потрапити приклади, а не всі входження"
    assert '"duration_ms":0.4' not in seen, "сирі JSON-рядки логів не мають доходити до моделі"


def test_pii_from_logs_is_redacted_before_the_model():
    """IP і пошта приходять у контекст з виводів тулів, а не від користувача."""
    agent, model = build([
        call("query_loki_patterns", {"service": "demo-chaos-svc"}),
        AIMessage(content="висновок"),
    ])
    agent.invoke({"messages": [{"role": "user", "content": "алерт"}]})

    seen = "".join(str(m.content) for messages in model.calls for m in messages)
    assert "10.4.2.11" not in seen, "IP з лог-рядка мав бути замаскований"


def test_step_limit_is_enforced():
    """Нескінченний цикл тулів має впертись у стелю, а не крутитись вічно."""
    from agents.incident_agent import RUN_LIMIT

    agent, model = build([call("get_deploys", {"service": "demo-chaos-svc"}, "c1")])
    agent.invoke({"messages": [{"role": "user", "content": "алерт"}]})
    assert len(model.calls) <= RUN_LIMIT, f"{len(model.calls)} викликів моделі при ліміті {RUN_LIMIT}"


def test_propose_action_requires_human_approval():
    """HITL: пропозиція дії зупиняє граф на interrupt, а не виконується."""
    from langgraph.checkpoint.memory import InMemorySaver

    agent, _ = build([
        call("propose_action", {"service": "demo-chaos-svc", "action": "відкотити на 1.4.2",
                                "reason": "регресія релізу",
                                "command": "kubectl rollout undo deploy/demo-chaos-svc"}),
        AIMessage(content="готово"),
    ], checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "incident-1"}}
    result = agent.invoke({"messages": [{"role": "user", "content": "алерт"}]}, config=config)

    assert "__interrupt__" in result, "propose_action мав зупинити граф і чекати людину"
    tool_messages = [m for m in result.get("messages", []) if m.type == "tool"]
    assert not tool_messages, "жоден write-тул не мав виконатись до підтвердження"


def test_prompt_states_the_non_negotiables():
    from agents.incident_agent import SYSTEM_PROMPT

    assert "недовірений текст" in SYSTEM_PROMPT, "правило про prompt injection у логах"
    assert "unknown" in SYSTEM_PROMPT, "чесне 'не знаю' має бути дозволене явно"
    assert "propose_action" in SYSTEM_PROMPT


def test_prompt_demands_executable_actions_not_intentions():
    """Промах, який показав перший повний прогін: correctness 0.96, actionability 0.48.

    Агент майже завжди правильно називав причину і підкріплював її доказами — і майже
    ніколи не доводив справу до дій: «розглянути відкат», «виконати кроки з runbook»,
    «перевірити логи». Промпт вимагав брати дії з runbook, але не вимагав, щоб їх можна
    було виконати.
    """
    from agents.incident_agent import SYSTEM_PROMPT

    # Промпт перенесений по рядках, тому порівнюємо на згорнутих пробілах.
    flat = " ".join(SYSTEM_PROMPT.split())

    assert "recommended_actions" in flat
    assert "не ставлячи жодного уточнювального питання" in flat, \
        "критерій виконуваності має бути в промпті явним"
    for anti_pattern in ("розглянути відкат", "перевірити логи", "виконати кроки з runbook"):
        assert anti_pattern in flat, (
            f"промпт має назвати намір-заглушку {anti_pattern!r} прямо: загальне "
            f"«будь конкретним» модель ігнорує")
