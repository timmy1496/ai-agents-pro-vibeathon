"""Перевірка Slack-інтеграції перед демо: токен, канал, права, тестове повідомлення.

Дешевше за п'ять хвилин на сцені з мовчазним ботом.
"""
import sys

from agents.config import SLACK_CHANNEL
from agents.tools import slack


def main() -> int:
    from agents.config import SLACK_BOT_TOKEN

    problem = slack.check_token(SLACK_BOT_TOKEN, "xoxb-", "SLACK_BOT_TOKEN")
    if problem:
        print(problem)
        print("без робочого токена агент пише у файл-емуляцію")
        return 1

    print(f"канал: {SLACK_CHANNEL}")
    result = slack.post("slack-check", ":wrench: перевірка інтеграції SRE-агента")
    if "error" in result:
        print(f"ПОМИЛКА: {result['error']}")
        if result.get("hint"):
            print(f"  -> {result['hint']}")
        return 2

    print(f"повідомлення відправлено: ts={result['ts']}, канал={result['channel']}")
    reply = slack.post("slack-check", "відповідь у тред — тредінг працює")
    if "error" in reply:
        print(f"тред НЕ працює: {reply['error']}")
        return 3

    print(f"відповідь у тред: thread_ts={reply['thread_ts']}")
    print("Slack готовий до демо")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
