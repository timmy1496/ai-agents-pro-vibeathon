"""Модель поверх Claude Code CLI — щоб евали бігали на підписці, без API-ключа.

Навіщо. Прогін датасету коштує грошей на API-ключі, а підписка Claude Code вже
оплачена. `claude -p` (headless-режим) — документований програмний вхід у неї, тому
транспорт до моделі можна замінити, не чіпаючи нічого іншого: агентський цикл
LangChain, middleware (PII, стеля кроків, guard на деструктив), критик і структурований
вивід лишаються рівно тими самими, що й у проді. Міняється тільки те, ЯК запит
доїжджає до моделі.

Ціна цього рішення, і її треба назвати вголос.

**Tool calling тут промптовий, а не нативний.** CLI віддає текст, тому виклик
інструмента доводиться просити JSON-протоколом і парсити. Нативний tool calling
провайдера — інший механізм з іншою поведінкою на межах: інакше ламається на довгих
аргументах, інакше обирає між кількома інструментами, інакше тримається схеми. Тому
оцінки, зняті через цей провайдер, НЕ порівнюються з оцінками, знятими через API —
так само, як не порівнюються оцінки різних суддів. Провайдер потрапляє в meta звіту,
і дельта між різними провайдерами не рахується (див. evals/run.py).

**Один виклик — один процес** (~5-7 с накладних). Для датасету на 14 кейсів це
хвилини, для проду — ні; у проді лишається звичайний API-клієнт.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

CLI = "claude"
DEFAULT_TIMEOUT = 300

# Протокол відповіді. Свідомо один об'єкт із фіксованим порядком ключів: reasoning
# перед рішенням, з тієї ж причини, що й у судді евалів — рішення, ВИВЕДЕНЕ з
# міркування, і рішення, обґрунтоване після, це різні речі.
# Це не частина протоколу, а факт про середовище, тому воно окремо і йде в КОЖЕН
# виклик — включно з тими, де інструментів не біндили.
#
# `--allowed-tools ""` знімає ДОЗВІЛ на вбудовані інструменти Claude Code, але не
# прибирає їх зі схеми: модель їх бачить і тягнеться до Read чи Bash, а це миттєвий
# вихід з кодом 1 і stop_reason "tool_use". Один раз ця фраза вже губилась — у кроці
# перекладу прози у форму, де tools порожній, — і кейс евала впав саме на цьому.
NO_ENV_TOOLS = """У тебе НЕМАЄ жодних інструментів середовища: ти не читаєш файли,
не запускаєш команди, не ходиш у мережу. Спроба скористатись ними обриває виклик
помилкою. Усе потрібне вже є в тексті запиту."""

PROTOCOL = """Ти працюєш як МОДЕЛЬ усередині чужого агентського циклу, а не як агент.

Єдиний спосіб щось зробити — назвати інструмент у JSON нижче; виконає його той цикл,
що тебе викликав, і поверне результат наступним повідомленням.

Твоя відповідь — РІВНО один JSON-об'єкт і нічого крім нього. Без markdown-огорожі,
без тексту до або після.

Два дозволені формати.

1. Викликати інструменти:
{"reasoning": "<чому саме ці інструменти, 1-2 речення>", "tool_calls": [{"name": "<ім'я>", "args": {<аргументи>}}]}

2. Дати остаточну відповідь:
{"reasoning": "<як ти дійшов висновку, 1-2 речення>", "content": "<відповідь текстом>"}

Правила:
- name має бути РІВНО з переліку доступних інструментів нижче;
- args мають відповідати схемі інструмента: типи, обов'язкові поля, жодних вигаданих ключів;
- за один крок можна викликати кілька інструментів, якщо вони незалежні;
- ключ "reasoning" завжди перший."""

TOOL_CHOICE_FORCED = """
- На ЦЬОМУ кроці формат 2 заборонений: ти зобов'язаний повернути tool_calls.
- Інструмент тут рівно один — {name}. Він і є форма твоєї відповіді: поклади її поля
  в args і нічого туди більше не додавай."""


class ClaudeCodeError(RuntimeError):
    """CLI не відповів або відповів не за протоколом.

    `raw` несе те, що модель насправді сказала. Це не для логів: коли модель написала
    змістовну відповідь, але не в тій формі, з цього тексту ще можна врятувати
    результат — і краще перекласти його у форму, ніж викидати роботу і перепитувати.
    """

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class ToolAttempt(ClaudeCodeError):
    """Модель потягнулась до вбудованого інструмента Claude Code, і CLI обірвався.

    Окремий тип, бо лікується інакше: тут не «відповідь не тієї форми», а «модель
    вирішила, що в неї є середовище». Нагадування про формат тут не діє — діє
    нагадування про те, що інструментів у неї немає.
    """


def _strip_fence(text: str) -> str:
    """Модель час від часу загортає JSON у ```json — знімаємо, це не помилка формату."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    return fenced.group(1) if fenced else text


def _first_object(text: str) -> dict:
    body = _strip_fence(text).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # Останній шанс: витягти найбільший збалансований об'єкт. Потрібен рідко, але
    # падати через один зайвий рядок пояснення дорожче, ніж його пережити.
    start = body.find("{")
    if start == -1:
        raise ClaudeCodeError(f"у відповіді немає JSON: {body[:400]!r}", raw=text)
    depth, in_string, escaped = 0, False, False
    for index, char in enumerate(body[start:], start):
        if in_string:
            in_string, escaped = (in_string and not (char == '"' and not escaped)), \
                (char == "\\" and not escaped)
            continue
        if char == '"':
            in_string, escaped = True, False
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = body[start:index + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as error:
                    # Найчастіше — Python-словник замість JSON: одинарні лапки, True/None.
                    # Голий JSONDecodeError звідси летів повз ретрай, бо той ловив лише
                    # ClaudeCodeError, і кейс евала помирав на дрібниці форматування.
                    raise ClaudeCodeError(
                        f"схоже на об'єкт, але не JSON: {candidate[:300]!r} ({error})",
                        raw=text) from error
    raise ClaudeCodeError(f"незакритий JSON: {body[:400]!r}", raw=text)


def _render(message: BaseMessage) -> str:
    """Історія діалогу в текст: CLI приймає один промпт, а не список повідомлень."""
    if message.type == "tool":
        return f"[РЕЗУЛЬТАТ ІНСТРУМЕНТА {message.name}]\n{message.content}"
    if message.type == "ai":
        calls = getattr(message, "tool_calls", None)
        if calls:
            rendered = json.dumps(
                [{"name": c["name"], "args": c["args"]} for c in calls], ensure_ascii=False)
            return f"[ТИ ВИКЛИКАВ ІНСТРУМЕНТИ]\n{rendered}"
        return f"[ТИ]\n{message.content}"
    return f"[КОРИСТУВАЧ]\n{message.content}"


def _usage(usage: dict) -> dict:
    tokens_in, tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return {"usage_metadata": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                               "total_tokens": tokens_in + tokens_out}}


def _tool_contract(tools: list[dict]) -> str:
    lines = ["ДОСТУПНІ ІНСТРУМЕНТИ:"]
    for spec in tools:
        function = spec["function"]
        lines.append(f"\n### {function['name']}\n{function.get('description', '').strip()}\n"
                     f"схема аргументів: {json.dumps(function.get('parameters', {}), ensure_ascii=False)}")
    return "\n".join(lines)


class ClaudeCodeChatModel(BaseChatModel):
    """LangChain-модель, що ходить у підписку через `claude -p`."""

    model: str = "claude-sonnet-5"
    timeout: int = DEFAULT_TIMEOUT
    tools: list[dict] = Field(default_factory=list)
    force_tool_call: bool = False
    # Кожен виклик — окремий процес, тому сумарну вартість збираємо самі: CLI віддає
    # її за прейскурантом, і на підписці це не рахунок, а порядок величини.
    spend: dict = Field(default_factory=lambda: {"usd": 0.0, "calls": 0,
                                                 "input": 0, "output": 0})

    @property
    def _llm_type(self) -> str:
        return "claude-code-cli"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None,
                   **kwargs: Any) -> "ClaudeCodeChatModel":
        del kwargs  # ls_structured_output_format тощо — нам не потрібні
        converted = [convert_to_openai_tool(t) for t in tools]
        bound = self.model_copy(update={
            "tools": converted,
            "force_tool_call": tool_choice in ("any", "required") or isinstance(tool_choice, dict),
        })
        # spend спільний з батьком: інакше витрати зв'язаної моделі загубились би
        bound.spend = self.spend
        return bound

    def _system_prompt(self, messages: list[BaseMessage]) -> str:
        parts = [str(m.content) for m in messages if m.type == "system"]
        if not self.tools:
            # Без інструментів емулювати нічого: це звичайне текстове доповнення, і
            # протокол тут не просто зайвий, а шкідливий — у судді евалів свій контракт
            # відповіді ({rationale, score}), і два JSON-контракти в одному промпті
            # модель зводить у щось третє. Прибираємо протокол, але НЕ факт про
            # середовище: він потрібен завжди.
            return "\n\n---\n\n".join([*parts, NO_ENV_TOOLS])
        forced = ""
        if self.force_tool_call:
            names = [t["function"]["name"] for t in self.tools]
            forced = TOOL_CHOICE_FORCED.format(
                name=names[0] if len(names) == 1 else "один із перелічених нижче")
        parts.append(NO_ENV_TOOLS + "\n\n" + PROTOCOL + forced)
        if self.tools:
            parts.append(_tool_contract(self.tools))
        return "\n\n---\n\n".join(parts)

    def _run_cli(self, system: str, prompt: str) -> dict:
        if shutil.which(CLI) is None:
            raise ClaudeCodeError(
                f"{CLI} не знайдено в PATH. Цей провайдер ходить у підписку через "
                f"Claude Code CLI; або встанови його, або задай ANTHROPIC_API_KEY.")

        # І системний промпт, і сам запит легко переростають ARG_MAX (лог інструментів
        # з логами Loki — це десятки кілобайт), тому перший іде файлом, другий — stdin.
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8",
                                         delete=False) as handle:
            handle.write(system)
            system_file = handle.name

        command = [
            CLI, "-p",
            "--system-prompt-file", system_file,
            "--model", self.model,
            "--output-format", "json",
            # 4, а не 1. --allowed-tools "" знімає ДОЗВІЛ на вбудовані інструменти, але
            # не прибирає їх зі схеми: модель їх бачить і час від часу тягнеться до Read
            # чи Bash, а на --max-turns 1 це миттєвий вихід з кодом 1 і stop_reason
            # "tool_use". Запас у кілька обертів дає їй отримати відмову і відповісти
            # за протоколом. Головне лікування — сам протокол, який каже, що
            # інструментів у неї немає; це страховка на випадок, коли не подіяло.
            "--max-turns", "4",
            # Модель тут — саме модель, а не агент: свої інструменти, свої налаштування
            # і свої MCP-сервери Claude Code до цього виклику не додає.
            "--allowed-tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
        ]
        try:
            completed = subprocess.run(  # noqa: S603 — команда фіксована, дані йдуть stdin
                command, input=prompt, capture_output=True, text=True,
                timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as expired:
            raise ClaudeCodeError(f"{CLI} не відповів за {self.timeout} с") from expired
        finally:
            import os
            os.unlink(system_file)

        if completed.returncode != 0:
            # Найчастіша причина ненульового коду — не помилка транспорту, а спроба
            # моделі скористатись вбудованим інструментом Claude Code. Розрізняти їх
            # обов'язково: перше лікується повтором з іншим нагадуванням, друге —
            # нічим. Без розрізнення в лог летить сирий JSON CLI, а справжня причина
            # губиться в ньому (це вже коштувало кейса cfg-02 двічі).
            try:
                aborted = json.loads(completed.stdout).get("stop_reason") == "tool_use"
            except (json.JSONDecodeError, AttributeError):
                aborted = False
            if aborted:
                raise ToolAttempt(
                    "модель спробувала скористатись вбудованим інструментом Claude Code "
                    "замість протоколу — CLI обірвав виклик")

            detail = (completed.stderr or completed.stdout or "(порожньо)").strip()
            raise ClaudeCodeError(
                f"{CLI} вийшов з кодом {completed.returncode}. "
                f"промпт {len(prompt)} символів, системний {len(system)}. {detail[:800]}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ClaudeCodeError(f"{CLI} віддав не JSON: {completed.stdout[:400]!r}") from error
        if payload.get("is_error"):
            raise ClaudeCodeError(f"{CLI}: {payload.get('result', payload)}")

        # Облік саме тут, а не після розбору: невдала спроба теж витратила токени,
        # і ховати її з підсумку означало б занижувати вартість прогону.
        usage = payload.get("usage", {})
        self.spend["usd"] += payload.get("total_cost_usd", 0.0)
        self.spend["calls"] += 1
        self.spend["input"] += usage.get("input_tokens", 0)
        self.spend["output"] += usage.get("output_tokens", 0)
        return payload

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None,
                  **kwargs: Any) -> ChatResult:
        del stop, run_manager, kwargs

        conversation = [m for m in messages if m.type != "system"]
        prompt = "\n\n".join(_render(m) for m in conversation)
        system = self._system_prompt(messages)

        def attempt(text: str) -> AIMessage:
            payload = self._run_cli(system, text)
            result = str(payload.get("result", ""))
            if not self.tools:  # сирий текст, як його попросив викликач
                return AIMessage(content=result,
                                 response_metadata=_usage(payload.get("usage", {})))
            return self._to_message(_first_object(result), payload.get("usage", {}))

        try:
            return ChatResult(generations=[ChatGeneration(message=attempt(prompt))])
        except ToolAttempt:
            # Не формат, а середовище: повторюємо з прямою вказівкою. Дві спроби, бо
            # модель із довгим extended thinking зривається сюди повторно, а втрачений
            # кейс коштує дорожче за два виклики.
            for _ in range(2):
                try:
                    return ChatResult(generations=[ChatGeneration(message=attempt(
                        f"{prompt}\n\n{NO_ENV_TOOLS}\n[СИСТЕМА] Попередня спроба "
                        f"обірвалась, бо ти викликав інструмент середовища. Їх немає. "
                        f"Єдина доступна дія — JSON за протоколом."))])
                except ToolAttempt:
                    continue
            raise
        except Exception as first:
            # Ловимо ВСЕ, не лише ClaudeCodeError. Провайдер зобов'язаний або віддати
            # придатне повідомлення, або впасти голосно після спроб — але не пускати
            # нагору випадковий виняток розбору, який уб'є кейс евала на дрібниці.
            message = self._recover(
                system, prompt,
                first if isinstance(first, ClaudeCodeError) else ClaudeCodeError(str(first)),
                attempt)

        return ChatResult(generations=[ChatGeneration(message=message)])

    def _recover(self, system: str, prompt: str, failure: ClaudeCodeError,
                 attempt: Any) -> AIMessage:
        """Друга спроба після зламаного протоколу — і вона не однакова для двох випадків.

        Найчастіший збій — останній крок агента: модель написала змістовний звіт, але
        прозою з markdown, бо їй так природніше, ніж класти текст у поля схеми. Просто
        перепитати «ще раз, але за формою» слабо працює: модель знову захоплюється
        відповіддю. Тому тут ОКРЕМИЙ вузький виклик, у якого одна робота — перекласти
        вже написаний текст у JSON за схемою. Це набагато простіше завдання, і воно
        зберігає роботу моделі замість того, щоб її викидати.

        Коли рятувати нема чого (немає тексту або інструментів кілька), лишається
        звичайне нагадування про формат.
        """
        if failure.raw and self.force_tool_call and self.tools:
            names = {spec["function"]["name"] for spec in self.tools}
            reformat = self.model_copy(update={"tools": [], "force_tool_call": False})
            reformat.spend = self.spend
            payload = reformat._run_cli(
                "Ти перекладаєш готову відповідь у виклик інструмента. Нічого не вигадуй, "
                "нічого не втрачай, нічого не оцінюй наново: усе потрібне вже є в тексті.\n"
                'Поверни РІВНО один JSON-об\'єкт {"name": "<інструмент>", "args": {…}} '
                "і нічого крім нього.",
                f"{_tool_contract(self.tools)}\n\nТЕКСТ, ЯКИЙ ТРЕБА ПЕРЕКЛАСТИ "
                f"У ВИКЛИК:\n{failure.raw}")
            call = _first_object(str(payload.get("result", "")))
            name, args = call.get("name"), call.get("args")
            if name not in names or not isinstance(args, dict):
                raise ClaudeCodeError(
                    f"переклад у виклик не вдався: {str(call)[:300]!r}") from failure
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args,
                             "id": "cc_reformat", "type": "tool_call"}],
                response_metadata=_usage(payload.get("usage", {})),
            )

        return attempt(
            f"{prompt}\n\n[СИСТЕМА] Попередня спроба не дала валідної відповіді "
            f"({failure}). Поверни РІВНО один JSON-об'єкт за протоколом і нічого більше: "
            f"жодного markdown, жодного тексту поза JSON.")

    def _to_message(self, decision: dict, usage: dict) -> AIMessage:
        metadata = _usage(usage)
        raw_calls = decision.get("tool_calls") or self._salvage_forced_call(decision)
        if raw_calls:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": call["name"],
                    "args": call.get("args") or {},
                    "id": f"cc_{index}_{abs(hash(json.dumps(call, sort_keys=True))) % 10**8}",
                    "type": "tool_call",
                } for index, call in enumerate(raw_calls)],
                response_metadata=metadata,
            )
        if self.force_tool_call:
            content = str(decision.get("content", ""))
            raise ClaudeCodeError(
                f"на цьому кроці був обов'язковий tool_call, а модель віддала текст: "
                f"{content[:300]!r}", raw=content)
        return AIMessage(content=str(decision.get("content", "")), response_metadata=metadata)

    def _matching_tool(self, fields: dict) -> str | None:
        """Якому інструменту належать ці поля.

        Покластись на «інструмент один» не можна, і це не теорія: у фінальному кроці
        create_agent зі структурованим виводом LangChain біндить УСІ інструменти агента
        плюс інструмент-схему відповіді, і ставить tool_choice="any". Тобто forced-виклик
        з одинадцятьма інструментами — не крайній випадок, а звичайний.

        Тому вибір за схемою: усі обов'язкові поля інструмента присутні, зайвих немає.
        Якщо підходить рівно один — форма відповіді однозначна.
        """
        keys = set(fields)
        matches = [
            spec["function"]["name"]
            for spec in self.tools
            if (params := spec["function"].get("parameters", {}))
            and (required := set(params.get("required", [])))
            and required <= keys <= set(params.get("properties", {}))
        ]
        return matches[0] if len(matches) == 1 else None

    def _salvage_forced_call(self, decision: dict) -> list[dict]:
        """Структурований вивід, поданий без обгортки протоколу.

        `with_structured_output` — це tool calling, і модель регулярно віддає його
        «навпростець»: кладе поля схеми в content об'єктом, кладе їх туди рядком, або
        взагалі повертає саму схему замість {reasoning, tool_calls}. Змісту це не
        змінює, і карати за це кейс евала немає сенсу.
        """
        if not self.force_tool_call:
            return []
        content = decision.get("content")
        if isinstance(content, str):
            try:  # схема, серіалізована в рядок усередині content
                content = json.loads(_strip_fence(content).strip())
            except json.JSONDecodeError:
                content = None
        envelope = {"reasoning", "content", "tool_calls"}
        fields = content if isinstance(content, dict) else \
            {k: v for k, v in decision.items() if k not in envelope}
        if not fields:
            return []
        name = self._matching_tool(fields)
        return [{"name": name, "args": fields}] if name else []

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator:
        raise NotImplementedError("headless-виклик не стрімиться, і евалам це не потрібно")


def build(model: str, temperature: float = 0.0) -> ClaudeCodeChatModel:
    """`anthropic:claude-sonnet-5` / `anthropic/claude-sonnet-5` -> `claude-sonnet-5`."""
    del temperature  # CLI не приймає temperature; для евалів це і не важіль
    bare = re.split(r"[:/]", model)[-1]
    return ClaudeCodeChatModel(model=bare)


def available() -> bool:
    return shutil.which(CLI) is not None


__all__ = ["ClaudeCodeChatModel", "ClaudeCodeError", "available", "build"]
