"""Тонкий клієнт Slack Web API.

Без slack_sdk: потрібен рівно один метод chat.postMessage, і це двадцять рядків
urllib проти ще однієї залежності.

Тред інциденту = тред у Slack. Перше повідомлення (сам алерт) створює тред і віддає
`ts`; усе далі — звіт, пропозиції, рішення людини — йде в нього через `thread_ts`.
Відповідність fingerprint алерту -> ts зберігається у файлі, щоб переживати рестарт агента.
"""
from __future__ import annotations

import json
import pathlib
import urllib.request

from agents.config import DATA_DIR, SLACK_BOT_TOKEN, SLACK_CHANNEL

API = "https://slack.com/api/chat.postMessage"
THREAD_MAP = DATA_DIR / "slack_thread_map.json"
# Slack ріже секцію на 3000 символах; лишаємо запас на розмітку
MAX_SECTION_CHARS = 2900


def enabled() -> bool:
    return bool(SLACK_BOT_TOKEN)


def _load_map() -> dict[str, str]:
    return json.loads(THREAD_MAP.read_text()) if THREAD_MAP.exists() else {}


def _save_map(mapping: dict[str, str]) -> None:
    THREAD_MAP.parent.mkdir(parents=True, exist_ok=True)
    THREAD_MAP.write_text(json.dumps(mapping, indent=2) + "\n")


def _blocks(text: str) -> list[dict]:
    """Ріже довгий звіт на секції — цілий RCA не влазить в одну."""
    chunks, current = [], ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 > MAX_SECTION_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]


def _call(payload: dict) -> dict:
    request = urllib.request.Request(
        API, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def remember_thread(thread_id: str, thread_ts: str) -> None:
    """Прив'язує наш ідентифікатор до наявного треда Slack.

    Потрібно, коли тред створив не агент, а людина — згадкою бота в каналі.
    """
    mapping = _load_map()
    if mapping.get(thread_id) != thread_ts:
        mapping[thread_id] = thread_ts
        _save_map(mapping)


def post(thread_id: str, text: str, channel: str = "") -> dict:
    """Пише в тред інциденту, створюючи його першим повідомленням.

    thread_id — наш ідентифікатор (fingerprint алерту або ts треда), не обов'язково
    Slack-івський.
    """
    mapping = _load_map()
    payload = {
        "channel": channel or SLACK_CHANNEL,
        "text": text[:200],          # fallback для нотифікацій і скрінрідерів
        "blocks": _blocks(text),
    }
    if thread_id in mapping:
        payload["thread_ts"] = mapping[thread_id]

    response = _call(payload)
    if not response.get("ok"):
        # Slack віддає 200 з ok=false — мовчазний провал, якщо не перевіряти
        return {"error": f"slack: {response.get('error', 'unknown')}",
                "hint": _hint(response.get("error", ""))}

    if thread_id not in mapping:
        mapping[thread_id] = response["ts"]
        _save_map(mapping)
    return {"posted": True, "channel": response["channel"], "ts": response["ts"],
            "thread_ts": mapping[thread_id]}


HINTS = {
    "not_in_channel": "бота не запрошено в канал: /invite @<bot> у потрібному каналі",
    "channel_not_found": "канал не існує або приватний і бота туди не додано",
    "invalid_auth": "токен недійсний — потрібен bot token, що починається з xoxb-",
    "missing_scope": "бракує скоупу chat:write у Bot Token Scopes",
    "not_authed": "SLACK_BOT_TOKEN порожній",
}


def _hint(error: str) -> str:
    return HINTS.get(error, "")
