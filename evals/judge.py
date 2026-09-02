"""LLM-judge: якісна оцінка звіту. Бігає на реліз, не на кожен коміт.

Дешева модель — суддя не міркує, а звіряє звіт з еталоном і виводами тулів.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.config import CHEAP_MODEL

Score = Literal[1, 2, 3, 4, 5]


class JudgeVerdict(BaseModel):
    correctness: Score = Field(description="Чи правильна корене­ва причина і чи узгоджена з доказами")
    groundedness: Score = Field(description="Чи кожен факт звіту спирається на вивід тула")
    actionability: Score = Field(description="Чи можна за рекомендаціями діяти без додаткових питань")
    reason: str = Field(description="Одне-два речення: що саме знизило оцінку")


JUDGE_PROMPT = """Ти оцінюєш звіт RCA за трьома шкалами 1-5.

correctness: клас причини правильний і випливає з доказів. Чесне "unknown" при браку
доказів — це 5, а вгадана причина без доказів — 1, навіть якщо вгадано правильно.
groundedness: кожен факт має посилання на конкретний PromQL/LogQL/шлях у KB. Факт без
джерела — максимум 2.
actionability: дії конкретні, з runbook, у правильному порядку. "Розібратись у проблемі" — 1.

Суди строго. Оцінка 5 означає "я б це відправив у прод-канал без правок"."""


def judge(case: dict, report, tool_log: str, model: str = CHEAP_MODEL) -> JudgeVerdict:
    from langchain.chat_models import init_chat_model

    grader = init_chat_model(model).with_structured_output(JudgeVerdict)
    return grader.invoke([
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content":
            f"ВХІД: {case['input']}\n"
            f"ЕТАЛОННИЙ КЛАС ПРИЧИНИ: {case['expected_root_cause']}\n"
            f"ОЧІКУВАНІ ТУЛИ: {case.get('expect_tools', [])}\n\n"
            f"ЗВІТ АГЕНТА:\n{report.model_dump_json(indent=2)}\n\n"
            f"ВИВОДИ ІНСТРУМЕНТІВ:\n{tool_log[:8000]}"},
    ])
