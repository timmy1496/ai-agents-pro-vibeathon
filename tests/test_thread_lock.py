"""Гонка, що з'їла звіт RCA на демо.

Жарт, замовлений посеред розслідування, дописав свій результат до чекпоінта,
знятого ДО публікації звіту, і звіт зник з пам'яті треда. Наступне «а чи було
таке раніше?» прийшло в контекст із самих жартів — і агент чесно відповів,
що не розуміє, про що йдеться.
"""
import threading

from agents.threadlock import thread_lock

THREAD = "інц-гонка"


def test_report_survives_a_question_asked_mid_investigation(monkeypatch):
    import agents.app as app_module
    import agents.slack_bot as slack_bot
    import agents.supervisor as supervisor_module
    from agents.incident_agent import RCAReport, Verdict
    from agents.tools import slack

    monkeypatch.setattr(app_module, "_annotate", lambda service, text: None)

    report = RCAReport(service="demo-chaos-svc", root_cause_label="release",
                       hypothesis="регресія в v1.5.0", confidence=0.93,
                       evidence=[], recommended_actions=["відкотити"])
    asking = threading.Event()      # другий прогін ось-ось стартує
    published = threading.Event()   # звіт опубліковано і записаний у пам'ять

    def fake_investigate(alert, config=None, on_report=None, **kwargs):
        asking.wait(5)
        on_report(report)
        published.set()
        return {"report": report, "verdict": Verdict(grounded=True, verdict="ACCEPT"),
                "revisions": 0, "state": {"messages": []}}

    monkeypatch.setattr("agents.incident_agent.investigate", fake_investigate)

    # другий «процес»: власний граф, спільний checkpointer — як слухач Slack поряд
    # з вебхуком. Воркер дочікується публікації, щоб гарантовано записатись ПІСЛЯ неї.
    monkeypatch.setattr(supervisor_module, "route",
                        lambda state: {"intent": "JOKE", "service": ""})
    saw_report = []

    def joke_node(state):
        published.wait(5)
        # Якщо розслідування не тримає лок від старту, воно застрягне на записі
        # памʼяті, поки цей прогін не відпустить тред: жарт відповість раніше звіту.
        saw_report.append(published.is_set())
        return {"messages": [{"role": "assistant", "content": ":coffee: жарт"}]}

    monkeypatch.setattr(supervisor_module, "joke_node", joke_node)
    listener = supervisor_module.build_supervisor()

    # згадка приходить у Slack-тред інциденту — саме той шлях, що зламався на демо
    slack.remember_thread(THREAD, "1700.001")

    def ask() -> None:
        asking.set()
        slack_bot.handle_event({"channel": "C1", "ts": "1700.001", "text": "пожартуй"},
                               listener, lambda **kwargs: None)

    question = threading.Thread(target=ask)
    question.start()
    app_module._investigate_and_post("HighErrorRate critical на demo-chaos-svc: 34%", THREAD)
    question.join(10)

    memory = [str(m.content) for m in app_module.supervisor.get_state(
        {"configurable": {"thread_id": THREAD}}).values["messages"]]
    assert any("регресія в v1.5.0" in m for m in memory), \
        f"звіт затерто паралельним прогоном: {memory}"
    assert any("жарт" in m for m in memory), "відповідь на запитання теж має лишитись"
    assert saw_report == [True], \
        "згадка мала дочекатись звіту, а не блокувати розслідування на записі памʼяті"


def test_lock_is_reentrant_within_one_thread():
    """Розслідування тримає лок і всередині дописує пам'ять — вкладений виклик
    не має заблокувати сам себе."""
    with thread_lock(THREAD):
        with thread_lock(THREAD):
            pass


def test_lock_is_exclusive_between_threads():
    order = []
    inside = threading.Event()
    with thread_lock(THREAD):
        def contend() -> None:
            inside.set()
            with thread_lock(THREAD):
                order.append("другий")

        other = threading.Thread(target=contend)
        other.start()
        inside.wait(5)
        order.append("перший")
    other.join(5)
    assert order == ["перший", "другий"]
