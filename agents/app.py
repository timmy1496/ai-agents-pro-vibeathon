"""HTTP-вхід агента: вебхук Alertmanager, /sre команда і перегляд Slack-тредів.

Slack емулюється файлом data/slack_threads.json і сторінкою на http://localhost:8000 —
формат повідомлень той самий, що пішов би в реальний workspace.
"""
from __future__ import annotations

import html
import json

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agents.observability import trace_config
from agents.config import DATA_DIR
from agents.supervisor import build_supervisor
from agents.tools.actions import SLACK_FILE, post_slack

PROCESSED = DATA_DIR / "processed_alerts.json"

app = FastAPI(title="SRE Agent")
supervisor = build_supervisor()  # один checkpointer на процес: треди живуть між запитами


def _handle(text: str, thread_id: str) -> str:
    state = supervisor.invoke({"messages": [{"role": "user", "content": text}]},
                              config=trace_config(thread_id, tags=["sre-agent"]))
    answer = state["messages"][-1].content
    post_slack.invoke({"thread_id": thread_id, "text": answer})
    return answer


def _process_alert(summary: str, thread_id: str) -> None:
    """Оголошення алерту і розслідування — обидва у фоні.

    Публікація в Slack — синхронний мережевий виклик, і в async-ендпоінті вона
    блокує event loop. Alertmanager дає на нотифікацію кілька секунд і скасовує
    її по дедлайну, тож у самому обробнику не має лишитись ніякого вводу-виводу.
    """
    post_slack.invoke({"thread_id": thread_id, "text": f":rotating_light: {summary}"})
    _handle(summary, thread_id)


@app.post("/webhook/alert")
async def alertmanager_webhook(payload: dict, background: BackgroundTasks) -> dict:
    """Вхід від Alertmanager. Відповідаємо одразу — розслідування йде у фоні.

    Alertmanager вважає доставку невдалою за таймаутом і починає ретраїти, а RCA
    триває десятки секунд. Тому підтверджуємо прийом, а не результат.
    """
    alerts = payload.get("alerts", [])
    threads, skipped = [], 0
    for alert in alerts:
        labels = alert.get("labels", {})
        status = alert.get("status", "firing")
        thread_id = _incident_id(alert, labels)
        if _already_handled(thread_id, status):
            skipped += 1
            continue

        summary = (f"{labels.get('alertname')} {labels.get('severity')} на "
                   f"{labels.get('service')}: {alert.get('annotations', {}).get('summary', '')}")
        if status == "resolved":
            # закриття інциденту — це один рядок у тред, а не привід розслідувати наново
            background.add_task(post_slack.invoke,
                                {"thread_id": thread_id,
                                 "text": f":white_check_mark: {labels.get('alertname')} "
                                         f"на {labels.get('service')} — вирішено"})
        else:
            background.add_task(_process_alert, summary, thread_id)
        threads.append(thread_id)
    return {"accepted": len(threads), "skipped_duplicates": skipped, "threads": threads}


def _already_handled(incident_id: str, status: str) -> bool:
    """Чи вже опрацьовано цей інцидент у цьому статусі.

    Alertmanager повторює нотифікацію кожен repeat_interval, поки алерт горить — це
    правильно з його боку (якщо агент лежав, повтор донесе алерт). Але для нас другий
    вебхук про той самий інцидент не є новиною: без цієї перевірки в тред щохвилини
    падав би новий звіт RCA. Стан на диску, щоб переживати рестарт агента.
    """
    processed = json.loads(PROCESSED.read_text()) if PROCESSED.exists() else {}
    if processed.get(incident_id) == status:
        return True
    processed[incident_id] = status
    PROCESSED.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED.write_text(json.dumps(processed, indent=2) + "\n")
    return False


def _incident_id(alert: dict, labels: dict) -> str:
    """Ідентифікатор інциденту = алерт + момент його початку.

    Сам fingerprint для цього не годиться: він стабільний для алерту, тому повторне
    спрацювання через годину лягло б у тред, створений минулого разу, і в каналі його
    ніхто б не побачив. startsAt змінюється з кожним новим загорянням після resolve,
    тож продовження того самого інциденту лишається в одному треді, а новий інцидент
    відкриває новий.
    """
    base = alert.get("fingerprint") or f"{labels.get('alertname')}-{labels.get('service')}"
    started = alert.get("startsAt", "")[:19]  # до секунд, без часового поясу
    return f"{base}-{started}" if started else base


class Command(BaseModel):
    text: str
    thread_id: str = "manual"


@app.post("/sre")
async def sre_command(command: Command) -> dict:
    """Емуляція слеш-команди / згадки в треді."""
    return {"thread_id": command.thread_id, "answer": _handle(command.text, command.thread_id)}


class Approval(BaseModel):
    thread_id: str
    approved: bool
    note: str = ""


@app.post("/approve")
async def approve(approval: Approval) -> dict:
    """Кнопка «підтвердити» під пропозицією дії.

    Рішення фіксується у треді. Саму зміну в інфраструктурі агент не виконує ніколи —
    підтвердження означає «людина прийняла рекомендацію», а не «агенту дозволено діяти».
    """
    verdict = "підтверджено" if approval.approved else "відхилено"
    post_slack.invoke({"thread_id": approval.thread_id,
                       "text": f":white_check_mark: Людина: дію {verdict}. {approval.note}".strip()})
    return {"thread_id": approval.thread_id, "decision": verdict}


@app.get("/", response_class=HTMLResponse)
async def threads_view() -> str:
    threads = json.loads(SLACK_FILE.read_text()) if SLACK_FILE.exists() else {}
    if not threads:
        body = "<p class=empty>Тредів ще немає. Запусти <code>make incident-1</code>.</p>"
    else:
        body = "".join(
            f"<section><h2>#{html.escape(thread_id)}</h2>" + "".join(
                f"<article><b>{html.escape(m['author'])}</b>"
                f"<pre>{html.escape(m['text'])}</pre></article>" for m in messages
            ) + "</section>"
            for thread_id, messages in reversed(threads.items())
        )
    return f"""<!doctype html><meta charset=utf-8><title>SRE Agent — треди</title>
<style>
 body{{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}}
 h1{{font-size:1.3rem}} h2{{font-size:.95rem;color:#555;margin:1.5rem 0 .5rem}}
 article{{border-left:3px solid #ddd;padding:.4rem .8rem;margin:.4rem 0;background:#fafafa}}
 pre{{white-space:pre-wrap;margin:.3rem 0;font:inherit}} .empty{{color:#777}}
</style>
<h1>SRE Agent — Slack-треди (емуляція)</h1>{body}"""
