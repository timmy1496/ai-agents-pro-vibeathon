"""Write-тули: єдиний вихід агента назовні.

Rule of Two: агент читає чуже (логи, алерти) і бачить приватне (метрики, каталог),
тому назовні йому дозволено рівно два канали — Slack-тред і Grafana annotation.
Будь-яка дія в інфраструктурі йде через propose_action і не виконується агентом взагалі.
"""
from __future__ import annotations

import base64
import json
import urllib.request

from langchain_core.tools import tool

from agents.config import DATA_DIR, GRAFANA_AUTH, GRAFANA_URL
from agents.guardrails import BLOCK_REASON, DESTRUCTIVE, is_destructive

SLACK_FILE = DATA_DIR / "slack_threads.json"

__all__ = ["DESTRUCTIVE", "create_annotation", "post_slack", "propose_action"]


def _post(url: str, payload: dict, headers: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


@tool
def post_slack(thread_id: str, text: str) -> dict:
    """Публікує повідомлення у Slack-тред інциденту.

    Стенд емулює Slack файлом data/slack_threads.json — формат повідомлення той самий,
    тому перехід на реальний workspace не змінює виклик.
    """
    threads = json.loads(SLACK_FILE.read_text()) if SLACK_FILE.exists() else {}
    threads.setdefault(thread_id, []).append({"author": "sre-agent", "text": text})
    SLACK_FILE.write_text(json.dumps(threads, indent=2, ensure_ascii=False) + "\n")
    return {"posted": True, "thread_id": thread_id, "messages_in_thread": len(threads[thread_id])}


@tool
def create_annotation(service: str, text: str, tags: list[str] | None = None) -> dict:
    """Ставить annotation у Grafana — слід розслідування на графіках сервісу."""
    auth = base64.b64encode(GRAFANA_AUTH.encode()).decode()
    try:
        response = _post(f"{GRAFANA_URL}/api/annotations",
                         {"text": text, "tags": [*(tags or []), service, "sre-agent"]},
                         {"Authorization": f"Basic {auth}"})
    except OSError as error:
        return {"error": f"grafana недоступна: {error}"}
    return {"created": True, "id": response.get("id"), "text": text}


@tool
def propose_action(service: str, action: str, reason: str, command: str = "") -> dict:
    """Пропонує дію людині. Агент її НЕ виконує — лише формулює і чекає підтвердження.

    action — що зробити словами ("відкотити на 1.4.2"), command — конкретна команда,
    якщо вона є.

    Деструктив відсікає DestructiveActionGuard ще до того, як цей код виконається —
    у графі агента сюди деструктивна команда не доходить взагалі. Перевірка лишається
    другим шаром: тул викликають і напряму (евали, /sre, майбутні інтеграції), а політика
    не має залежати від того, хто саме його покликав.
    """
    if is_destructive({"command": command, "action": action}):
        return {"blocked": True, "reason": BLOCK_REASON, "command": command}
    return {"status": "awaiting_human_approval", "service": service,
            "action": action, "reason": reason, "command": command}
