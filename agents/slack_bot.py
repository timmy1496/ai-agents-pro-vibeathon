"""Двосторонній Slack через Socket Mode.

Socket Mode, а не Events API: Slack сам відкриває WebSocket до нас, тому не потрібні
ні публічний URL, ні ngrok, ні відкритий порт. Для демо з ноутбука це єдиний варіант,
який не розсипається від зміни мережі.

Що вміє: згадка бота в каналі або відповідь у треді -> A0 Supervisor -> відповідь
у той самий тред. thread_id для агента = ts треда Slack, тому пам'ять діалогу
(checkpointer) і тред у Slack — це те саме.
"""
from __future__ import annotations

import logging
import re

from agents.config import SLACK_APP_TOKEN, SLACK_BOT_TOKEN
from agents.observability import trace_config
from agents.supervisor import build_supervisor
from agents.tools import slack

log = logging.getLogger(__name__)
MENTION = re.compile(r"<@[A-Z0-9]+>")
THINKING = ":hourglass_flowing_sand: розбираюсь…"


def strip_mention(text: str) -> str:
    """Прибирає <@U123> зі звернення — моделі це шум, а не частина питання."""
    return MENTION.sub("", text).strip()


def handle_event(event: dict, supervisor, post) -> str | None:
    """Обробляє одну подію Slack. Повертає відповідь або None, якщо подія не наша.

    Винесено з мережевого циклу, щоб логіку можна було тестувати без Slack.
    """
    if event.get("bot_id") or event.get("subtype"):
        return None  # власні повідомлення і службові події ігноруємо, інакше цикл

    question = strip_mention(event.get("text", ""))
    if not question:
        return None

    channel = event["channel"]
    # відповідь завжди в тред: якщо згадка була в каналі, тредом стає саме це повідомлення
    thread_ts = event.get("thread_ts") or event["ts"]
    slack.remember_thread(thread_ts, thread_ts)

    post(channel=channel, thread_ts=thread_ts, text=THINKING)
    state = supervisor.invoke({"messages": [{"role": "user", "content": question}]},
                              config=trace_config(thread_ts, tags=["slack"]))
    answer = state["messages"][-1].content
    post(channel=channel, thread_ts=thread_ts, text=answer)
    return answer


def run() -> int:
    """Слухає Slack, поки не зупинять."""
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    problems = [p for p in (slack.check_token(SLACK_BOT_TOKEN, "xoxb-", "SLACK_BOT_TOKEN"),
                            slack.check_token(SLACK_APP_TOKEN, "xapp-", "SLACK_APP_TOKEN")) if p]
    if problems:
        for problem in problems:
            print(f"  {problem}")
        print("\nApp-level токен: Socket Mode -> Generate Token (скоуп connections:write)")
        return 2

    web = WebClient(token=SLACK_BOT_TOKEN)
    supervisor = build_supervisor()
    identity = web.auth_test()
    print(f"SRE Agent слухає Slack як {identity['user']} у {identity['team']}")

    def post(channel: str, thread_ts: str, text: str) -> None:
        web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                             text=text[:200], blocks=slack._blocks(text))

    def on_request(client: SocketModeClient, request: SocketModeRequest) -> None:
        # ACK одразу: Slack чекає підтвердження 3 секунди і ретраїть, а відповідь
        # агента триває десятки секунд — без цього кожен запит прийде тричі
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type != "events_api":
            return
        event = request.payload.get("event", {})
        if event.get("type") not in ("app_mention", "message"):
            return
        try:
            handle_event(event, supervisor, post)
        except Exception as error:  # інцидент не має вбивати слухача
            log.exception("помилка обробки події")
            post(channel=event.get("channel", ""),
                 thread_ts=event.get("thread_ts") or event.get("ts", ""),
                 text=f":warning: не вдалось обробити: {error}")

    socket = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web)
    socket.socket_mode_request_listeners.append(on_request)
    socket.connect()
    from threading import Event

    Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
