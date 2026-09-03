"""Провайдер поверх Claude Code CLI — розбір відповіді і межі протоколу.

CLI тут не запускається: підмінюється рівно один вихід у процес (`_run_cli`), як і в
решті тестів проєкту підмінюється рівно один вихід у мережу. Перевіряється те, на чому
цей провайдер реально ламався на живому прогоні — усі три випадки нижче трапились,
а не вигадані.
"""
import json

import pytest

from agents.providers import claude_code as cc


def payload(result: str, cost: float = 0.01) -> dict:
    return {"result": result, "total_cost_usd": cost,
            "usage": {"input_tokens": 10, "output_tokens": 20}}


class Scripted:
    """Модель зі сценарієм відповідей CLI замість самого CLI.

    Пара (модель, сценарій) окремим об'єктом, бо ClaudeCodeChatModel — pydantic-модель
    і довільного атрибута на неї не почепиш.
    """

    def __init__(self, built, script):
        self.model, self.script = built, script

    def invoke(self, *args, **kwargs):
        return self.model.invoke(*args, **kwargs)

    def bind(self, **update):
        bound = self.model.model_copy(update=update)
        bound.spend = self.model.spend
        return bound

    @property
    def spend(self):
        return self.model.spend


@pytest.fixture
def model(monkeypatch):
    built = cc.ClaudeCodeChatModel(model="test-model")
    script: list = []

    def fake_run(self, system, prompt):
        if not script:
            raise AssertionError("сценарій вичерпано — CLI покликали більше разів, ніж очікували")
        self.spend["calls"] += 1
        return payload(script.pop(0))

    monkeypatch.setattr(cc.ClaudeCodeChatModel, "_run_cli", fake_run)
    return Scripted(built, script)


def tool_spec(name: str = "get_deploys", properties: tuple = ("service", "hours"),
              required: tuple = ("service",)) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": "опис",
        "parameters": {"type": "object",
                       "properties": {p: {"type": "string"} for p in properties},
                       "required": list(required)}}}


VERDICT = tool_spec("Verdict", properties=("ok",), required=("ok",))


# --- розбір відповіді ---------------------------------------------------------

def test_tool_call_is_parsed(model):
    model.script.append(json.dumps({"reasoning": "треба глянути деплої",
                                    "tool_calls": [{"name": "get_deploys",
                                                    "args": {"service": "x"}}]}))
    bound = model.bind(tools=[tool_spec()])

    message = bound.invoke([{"role": "user", "content": "алерт"}])
    assert [c["name"] for c in message.tool_calls] == ["get_deploys"]
    assert message.tool_calls[0]["args"] == {"service": "x"}


def test_json_wrapped_in_a_markdown_fence_is_accepted(model):
    """Модель регулярно загортає JSON у ```json. Це не порушення змісту."""
    model.script.append('```json\n{"reasoning": "ок", "content": "готово"}\n```')
    bound = model.bind(tools=[tool_spec()])
    assert bound.invoke([{"role": "user", "content": "?"}]).content == "готово"


def test_prose_around_the_object_does_not_break_the_parse(model):
    model.script.append('Ось моя відповідь:\n{"reasoning": "r", "content": "готово"}\nСподіваюсь, підійде.')
    bound = model.bind(tools=[tool_spec()])
    assert bound.invoke([{"role": "user", "content": "?"}]).content == "готово"


# --- структурований вивід -----------------------------------------------------
#
# with_structured_output — це той самий tool calling з одним інструментом, і модель
# віддає його «навпростець» трьома різними способами. Усі три трапились на живому
# прогоні, тому всі три тут.

@pytest.mark.parametrize("answer,label", [
    ({"reasoning": "r", "tool_calls": [{"name": "Verdict", "args": {"ok": True}}]},
     "за протоколом"),
    ({"reasoning": "r", "content": {"ok": True}},
     "схема об'єктом у content"),
    ({"reasoning": "r", "content": '{"ok": true}'},
     "схема рядком у content"),
    ({"reasoning": "r", "ok": True},
     "схема прямо в корені, без обгортки"),
])
def test_single_forced_tool_accepts_every_shape_the_model_uses(model, answer, label):
    model.script.append(json.dumps(answer, ensure_ascii=False))
    bound = model.bind(tools=[VERDICT], force_tool_call=True)

    calls = bound.invoke([{"role": "user", "content": "?"}]).tool_calls
    assert [c["name"] for c in calls] == ["Verdict"], label
    assert calls[0]["args"] == {"ok": True}, label


def test_the_right_tool_is_picked_among_many_by_its_schema(model):
    """Головний виробничий випадок, і він не крайній.

    У фінальному кроці create_agent зі структурованим виводом LangChain біндить УСІ
    інструменти агента плюс інструмент-схему відповіді і ставить tool_choice="any".
    Тобто forced-виклик з одинадцятьма інструментами — норма, і припущення «інструмент
    один» тут просто не виконується.
    """
    model.script.append(json.dumps({"reasoning": "r", "content": {"ok": True}}))
    bound = model.bind(force_tool_call=True, tools=[
        tool_spec("get_deploys"), tool_spec("k8s_events"), VERDICT])

    calls = bound.invoke([{"role": "user", "content": "?"}]).tool_calls
    assert [c["name"] for c in calls] == ["Verdict"]


def test_ambiguous_fields_are_not_guessed(model):
    """Два інструменти з однаковою схемою — форма відповіді неоднозначна.

    Тут вгадувати не можна: обраний навмання інструмент виконає не ту дію. Замість
    цього виклик іде на переклад окремим кроком.
    """
    fields = json.dumps({"reasoning": "r", "content": {"service": "x"}})
    model.script.extend([fields, '{"name": "A", "args": {"service": "x"}}'])
    bound = model.bind(force_tool_call=True, tools=[tool_spec("A"), tool_spec("B")])

    calls = bound.invoke([{"role": "user", "content": "?"}]).tool_calls
    assert calls[0]["name"] == "A", "неоднозначність вирішує окремий крок перекладу"
    assert not model.script


# --- режими -------------------------------------------------------------------

def test_without_tools_the_protocol_is_not_injected(model):
    """У судді евалів свій контракт відповіді.

    Два JSON-контракти в одному системному промпті модель зводить у щось третє —
    саме на цьому падав суддя, поки провайдер нав'язував свій протокол усім.
    """
    from langchain_core.messages import SystemMessage

    model.script.append('{"rationale": "бо так", "score": 0.8}')
    system = model.model._system_prompt([SystemMessage(content="Ти суддя.")])

    assert "Ти суддя." in system
    assert "tool_calls" not in system, "протокол не має домішуватись до текстового виклику"
    assert "НЕМАЄ жодних інструментів середовища" in system, (
        "факт про середовище потрібен і без протоколу: інакше модель тягнеться до Read "
        "чи Bash і виклик обривається — так упав кейс cfg-02 на кроці перекладу")

    answer = model.invoke([{"role": "user", "content": "оціни"}])
    assert json.loads(answer.content)["score"] == 0.8, "сирий текст іде як є"


def test_with_tools_the_protocol_and_contract_are_present(model):
    from langchain_core.messages import SystemMessage

    bound = model.bind(tools=[tool_spec()])
    system = bound._system_prompt([SystemMessage(content="Ти агент.")])

    assert "Ти агент." in system
    assert "tool_calls" in system, "без протоколу модель не знає, як викликати інструмент"
    assert "get_deploys" in system, "контракт інструментів має бути у промпті"
    assert "НЕМАЄ жодних інструментів середовища" in system, (
        "без цього модель тягнеться до вбудованих інструментів Claude Code і виклик "
        "обривається помилкою")


# --- ретрай і облік -----------------------------------------------------------

def test_prose_is_translated_into_a_tool_call_not_just_re_asked(model):
    """Найдорожчий збій: модель написала змістовний звіт, але прозою.

    Просто перепитати «ще раз, але за формою» слабо працює — модель знову захоплюється
    відповіддю; на живому прогоні це двічі поспіль вбило кейс. Тому другий крок вузький:
    перекласти вже написаний текст у виклик, а не переробити роботу.
    """
    model.script.append(json.dumps({"reasoning": "r", "content": "## Звіт\n\nПричина — реліз."}))
    model.script.append('{"name": "Verdict", "args": {"ok": true}}')
    bound = model.bind(force_tool_call=True, tools=[VERDICT])

    calls = bound.invoke([{"role": "user", "content": "?"}]).tool_calls
    assert calls == [{"name": "Verdict", "args": {"ok": True},
                      "id": "cc_reformat", "type": "tool_call"}]
    assert not model.script


def test_translation_into_an_unknown_tool_is_refused(model):
    """Переклад не має права вигадати інструмент, якого не біндили."""
    model.script.append(json.dumps({"reasoning": "r", "content": "проза"}))
    model.script.append('{"name": "НеІснує", "args": {}}')
    bound = model.bind(force_tool_call=True, tools=[VERDICT])

    with pytest.raises(cc.ClaudeCodeError, match="переклад у виклик не вдався"):
        bound.invoke([{"role": "user", "content": "?"}])


def test_broken_protocol_gets_exactly_one_reminder_retry(model):
    model.script.append("зовсім не JSON")
    model.script.append('{"reasoning": "r", "content": "тепер за формою"}')
    bound = model.bind(tools=[tool_spec()])

    assert bound.invoke([{"role": "user", "content": "?"}]).content == "тепер за формою"
    assert not model.script, "друга спроба мала бути використана"


def test_two_failures_in_a_row_surface_as_an_error(model):
    model.script.extend(["не JSON", "теж не JSON"])
    bound = model.bind(tools=[tool_spec()])
    with pytest.raises(cc.ClaudeCodeError):
        bound.invoke([{"role": "user", "content": "?"}])


def test_raw_mode_never_retries_because_nothing_can_be_malformed(model):
    """Без інструментів контракту відповіді немає — будь-який текст валідний.

    Це не поблажливість, а межа відповідальності: формат тут вимагає той, хто кликав
    (суддя евалів), і перевіряє його теж він.
    """
    model.script.append("довільний текст без жодного JSON")
    assert model.invoke([{"role": "user", "content": "?"}]).content.startswith("довільний")
    assert not model.script, "жодної повторної спроби не мало бути"


def test_spend_counts_the_failed_attempt_too(monkeypatch):
    """Невдала спроба теж витратила токени. Ховати її з підсумку = занижувати вартість."""
    built = cc.ClaudeCodeChatModel(model="test-model")
    answers = ["не JSON", '{"reasoning": "r", "content": "ок"}']

    def fake_subprocess(*args, **kwargs):
        class Done:
            returncode = 0
            stdout = json.dumps(payload(answers.pop(0), cost=0.02))
            stderr = ""
        return Done()

    monkeypatch.setattr(cc.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(cc.shutil, "which", lambda _: "/usr/bin/claude")

    bound = built.model_copy(update={"tools": [tool_spec()]})
    bound.spend = built.spend
    bound.invoke([{"role": "user", "content": "?"}])
    assert built.spend["calls"] == 2, "лічильник має врахувати обидва виклики CLI"
    assert built.spend["usd"] == pytest.approx(0.04)


def test_bind_tools_shares_the_spend_counter():
    """Прив'язана копія — та сама модель. Інакше витрати агента загубились би."""
    from langchain_core.tools import tool

    @tool
    def noop(x: str) -> str:
        """тул."""
        return x

    built = cc.ClaudeCodeChatModel(model="test-model")
    bound = built.bind_tools([noop])
    bound.spend["calls"] += 1
    assert built.spend["calls"] == 1


def test_missing_cli_says_what_to_do(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda _: None)
    with pytest.raises(cc.ClaudeCodeError, match="ANTHROPIC_API_KEY"):
        cc.ClaudeCodeChatModel(model="m")._run_cli("sys", "prompt")


def test_model_id_is_stripped_of_the_provider_prefix():
    assert cc.build("anthropic:claude-sonnet-5").model == "claude-sonnet-5"
    assert cc.build("anthropic/claude-haiku-4.5").model == "claude-haiku-4.5"
