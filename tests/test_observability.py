"""Трейсинг не має права ламати інцидент."""


def test_trace_config_carries_thread_and_session(monkeypatch):
    import agents.observability as obs

    obs._handler.cache_clear()
    monkeypatch.setattr(obs, "_handler", lambda: None)
    config = trace = obs.trace_config("slack-C1-1700", tags=["sre-agent"], case="rel-01")

    assert config["configurable"]["thread_id"] == "slack-C1-1700"
    assert config["metadata"]["langfuse_session_id"] == "slack-C1-1700", \
        "сесія = тред, інакше інцидент розсиплеться на незв'язані трейси"
    assert config["metadata"]["case"] == "rel-01"
    assert "callbacks" not in trace, "без Langfuse конфіг має лишатись валідним"


def test_missing_langfuse_degrades_quietly(monkeypatch):
    import agents.observability as obs

    obs._handler.cache_clear()
    monkeypatch.setattr("agents.observability.LANGFUSE_ENABLED", True)

    def explode(*args, **kwargs):
        raise ConnectionError("langfuse недоступний")

    monkeypatch.setitem(__import__("sys").modules, "langfuse",
                        type("M", (), {"Langfuse": explode})())
    assert obs._handler() is None, "недоступний Langfuse має вимкнути трейсинг, а не впасти"


def test_disabled_by_env(monkeypatch):
    import agents.observability as obs

    obs._handler.cache_clear()
    monkeypatch.setattr("agents.observability.LANGFUSE_ENABLED", False)
    assert obs._handler() is None
