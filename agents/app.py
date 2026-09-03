"""HTTP-вхід агента: вебхук Alertmanager, /sre команда і перегляд Slack-тредів.

Slack емулюється файлом data/slack_threads.json і сторінкою на http://localhost:8000 —
формат повідомлень той самий, що пішов би в реальний workspace.
"""
from __future__ import annotations

import html
import json
import logging
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agents.config import AGENT_TOKEN, AGENT_TOKEN_IS_DEFAULT
from agents.observability import trace_config
from agents.supervisor import build_supervisor
from agents.tools.actions import SLACK_FILE, post_slack

log = logging.getLogger(__name__)

app = FastAPI(title="SRE Agent")
supervisor = build_supervisor()  # один checkpointer на процес: треди живуть між запитами


def require_token(authorization: str = Header(default="")) -> None:
    """Спільний секрет на всіх POST-ручках.

    Найважливіша з них — /approve: це кнопка HITL, і без неї «підтвердити дію на
    tier-1» міг будь-хто, хто дотягнувся до порту. compare_digest, а не ==, щоб
    порівняння не текло по часу.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, AGENT_TOKEN):
        raise HTTPException(status_code=401, detail="потрібен Authorization: Bearer <AGENT_TOKEN>")


if AGENT_TOKEN_IS_DEFAULT:
    log.warning("AGENT_TOKEN не заданий — діє демонстраційний дефолт. "
                "Для будь-чого, крім локального стенду, задай його явно.")


def _handle(text: str, thread_id: str) -> str:
    """Один прохід супервізора з відповіддю в тред.

    Помилка теж іде в тред, і це не косметика: розслідування бігає у фоні
    (див. вебхук), тому виняток тут нікому повернути — HTTP-відповідь Alertmanager
    отримав кілька десятків секунд тому. Без цього блоку скінчений ключ або
    недоступний провайдер виглядають як тред, у якому агент просто мовчить,
    а справжня причина лишається в stderr процесу.
    """
    try:
        state = supervisor.invoke({"messages": [{"role": "user", "content": text}]},
                                  config=trace_config(thread_id, tags=["sre-agent"]))
        answer = str(state["messages"][-1].content)
    except Exception as error:
        log.exception("розслідування у треді %s не завершилось", thread_id)
        answer = (f":warning: Розслідування не завершилось: {type(error).__name__}: {error}\n"
                  f"Алерт лишається відкритим — розбирає людина.")
    post_slack.invoke({"thread_id": thread_id, "text": answer})
    return answer


@app.post("/webhook/alert", dependencies=[Depends(require_token)])
async def alertmanager_webhook(payload: dict, background: BackgroundTasks) -> dict:
    """Вхід від Alertmanager. Відповідаємо одразу — розслідування йде у фоні.

    Alertmanager вважає доставку невдалою за таймаутом і починає ретраїти, а RCA
    триває десятки секунд. Тому підтверджуємо прийом, а не результат.
    """
    alerts = payload.get("alerts", [])
    threads = []
    for alert in alerts:
        labels = alert.get("labels", {})
        # fingerprint від Alertmanager стабільний для однієї групи -> один тред на інцидент
        thread_id = alert.get("fingerprint") or f"{labels.get('alertname')}-{labels.get('service')}"
        summary = (f"{labels.get('alertname')} {labels.get('severity')} на "
                   f"{labels.get('service')}: {alert.get('annotations', {}).get('summary', '')}")
        post_slack.invoke({"thread_id": thread_id, "text": f":rotating_light: {summary}"})
        background.add_task(_handle, summary, thread_id)
        threads.append(thread_id)
    return {"accepted": len(alerts), "threads": threads}


class Command(BaseModel):
    text: str
    thread_id: str = "manual"


@app.post("/sre", dependencies=[Depends(require_token)])
async def sre_command(command: Command) -> dict:
    """Емуляція слеш-команди / згадки в треді."""
    return {"thread_id": command.thread_id, "answer": _handle(command.text, command.thread_id)}


class Approval(BaseModel):
    thread_id: str
    approved: bool
    note: str = ""


@app.post("/approve", dependencies=[Depends(require_token)])
async def approve(approval: Approval) -> dict:
    """Кнопка «підтвердити» під пропозицією дії — і продовження перерваного графа.

    Раніше ця ручка лише писала в тред, а interrupt висів у checkpointer назавжди:
    петля HITL виглядала замкненою, але не була. Тепер рішення повертається в граф
    (`Command(resume=...)`), і агент доводить крок до кінця.

    Саму зміну в інфраструктурі агент не виконує ніколи: підтвердження означає «людина
    прийняла рекомендацію», а не «агенту дозволено діяти» — propose_action і після
    approve повертає пропозицію, а не результат виконання.
    """
    from agents.incident_agent import resume

    decision = "approve" if approval.approved else "reject"
    verdict = "підтверджено" if approval.approved else "відхилено"
    post_slack.invoke({"thread_id": approval.thread_id,
                       "text": f":white_check_mark: Людина: дію {verdict}. {approval.note}".strip()})

    try:
        state = resume(decision, approval.note,
                       config=trace_config(approval.thread_id, tags=["hitl"]))
    except Exception as error:  # тред без активного interrupt — нормальна ситуація
        log.info("нічого продовжувати у треді %s: %s", approval.thread_id, error)
        return {"thread_id": approval.thread_id, "decision": verdict, "resumed": False}

    answer = state["messages"][-1].content
    if answer:
        post_slack.invoke({"thread_id": approval.thread_id, "text": str(answer)})
    return {"thread_id": approval.thread_id, "decision": verdict, "resumed": True,
            "answer": answer}


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
