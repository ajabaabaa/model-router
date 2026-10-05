"""Caller attribution: the client hint is always recorded, and becomes a caller label only when
the client names itself or the operator mapped it."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import app
import attribution

CONFIG = {"tiers": {"fast": {"provider": "openrouter", "primary": "m1", "fallbacks": []},
                    "balanced": {"provider": "openrouter", "primary": "m2", "fallbacks": []}},
          "openrouter": {"base_url": "https://x/api/v1", "api_key_env": "OPENROUTER_KEY"},
          "client_labels": {"claude-code": "claude", "openai": "chatgpt", "openai-python": "gpt-sdk"}}


def post(monkeypatch, tmp_path, body, headers=None, config=CONFIG):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db"); app.init_db()
    monkeypatch.setenv("OPENROUTER_KEY", "k")
    monkeypatch.setattr(app, "load_config", lambda: config)

    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers, json): return Response()

    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json=body, headers=headers or {})
    assert r.status_code == 200
    with sqlite3.connect(tmp_path / "t.db") as db:
        return db.execute("SELECT agent, client_hint FROM telemetry ORDER BY id DESC LIMIT 1").fetchone()


@pytest.mark.parametrize("ua,hint", [
    ("OpenAI/Python 1.40.2", "openai"), ("python-httpx/0.27.0", "python-httpx"),
    ("Mozilla/5.0 (X11; Linux)", "mozilla"), ("claude-code/2.1 (cli)", "claude-code"),
    ("", None), (None, None), ("<script>alert(1)</script>", "script-alert"),
])
def test_client_hint_keeps_only_the_product_token_in_a_safe_alphabet(ua, hint):
    headers = {} if ua is None else {"user-agent": ua}
    assert attribution.client_hint(headers) == hint


def test_hint_is_length_capped_and_labels_are_sanitised():
    assert len(attribution.client_hint({"user-agent": "x" * 500})) == attribution.MAX_LEN
    assert attribution.sanitize_label("  My Agent!! ") == "my-agent"
    assert attribution.sanitize_label("!!!") is None and attribution.sanitize_label(5) is None


def test_label_for_prefers_the_longest_matching_key_and_needs_a_mapping():
    labels = CONFIG["client_labels"]
    assert attribution.label_for("openai-python", labels) == "gpt-sdk"
    assert attribution.label_for("openai", labels) == "chatgpt"
    assert attribution.label_for("curl", labels) is None
    assert attribution.label_for("openai", None) is None and attribution.label_for(None, labels) is None


def test_hint_is_recorded_but_never_becomes_the_caller_on_its_own(monkeypatch, tmp_path):
    agent, hint = post(monkeypatch, tmp_path, {"messages": []},
                       {"User-Agent": "mystery-client/3.1"})
    assert agent is None and hint == "mystery-client"


def test_operator_mapping_turns_a_hint_into_a_caller(monkeypatch, tmp_path):
    agent, hint = post(monkeypatch, tmp_path, {"model": "auto", "messages": []},
                       {"User-Agent": "claude-code/2.1"})
    assert (agent, hint) == ("claude", "claude-code")


def test_client_header_names_a_caller_with_no_config_and_is_sanitised(monkeypatch, tmp_path):
    agent, _ = post(monkeypatch, tmp_path, {"model": "auto", "messages": []},
                    {"X-Router-Client": "ChatGPT Actions", "User-Agent": "claude-code/2"})
    assert agent == "chatgpt-actions"  # the self-declared name beats the UA mapping


def test_existing_attribution_still_wins_over_the_new_routes(monkeypatch, tmp_path):
    assert post(monkeypatch, tmp_path, {"model": "auto", "messages": []},
                {"X-OpenClaw-Agent": "coordinator", "X-Router-Client": "other"})[0] == "coordinator"
    assert post(monkeypatch, tmp_path, {"model": "balanced#writer", "messages": []},
                {"X-Router-Client": "other"})[0] == "writer"
    # OpenHuman's bare-tier shape still resolves as before when nothing else names the caller
    assert post(monkeypatch, tmp_path, {"model": "balanced", "messages": []})[0] == "openhuman"


def test_missing_client_labels_in_config_is_fine(monkeypatch, tmp_path):
    bare = {k: v for k, v in CONFIG.items() if k != "client_labels"}
    assert post(monkeypatch, tmp_path, {"messages": []}, {"User-Agent": "claude-code/2"}, bare)[0] is None


def test_existing_database_gains_the_column_without_losing_rows(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
                   " request_id TEXT NOT NULL, selected_tier TEXT, actual_model TEXT, input_tokens INTEGER,"
                   " output_tokens INTEGER, latency_ms REAL NOT NULL, estimated_cost REAL, http_status INTEGER NOT NULL,"
                   " success INTEGER NOT NULL, fallback_count INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO telemetry (timestamp, request_id, latency_ms, http_status, success) VALUES ('t','r',1,200,1)")
    monkeypatch.setattr(app, "DB_PATH", path); app.init_db()
    with sqlite3.connect(path) as db:
        assert "client_hint" in {r[1] for r in db.execute("PRAGMA table_info(telemetry)")}
        assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 1


def test_provider_cached_tokens_and_session_are_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db"); app.init_db()
    monkeypatch.setenv("OPENROUTER_KEY", "k")
    monkeypatch.setattr(app, "load_config", lambda: CONFIG)

    class Response:
        status_code = 200
        def json(self):
            return {"provider": "DeepInfra", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 64}}}

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers, json): return Response()

    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"messages": []},
                                 headers={"X-Session-Id": "abc\x07123"})
    assert r.status_code == 200
    with sqlite3.connect(tmp_path / "t.db") as db:
        row = db.execute("SELECT upstream_provider, cached_tokens, session_id FROM telemetry").fetchone()
    assert row == ("DeepInfra", 64, "abc123")


def test_note_upstream_ignores_junk():
    meta = {}
    app.note_upstream(meta, {"provider": 5}, {"prompt_tokens_details": {"cached_tokens": "x"}})
    app.note_upstream(meta, None, None)
    assert meta == {}
