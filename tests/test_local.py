"""Local (Ollama) provider: request path, privacy label, config rails, installed-model picker, tier editor."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app
import catalog
import config_store
import dashboard
import local_models
import risk
from test_dashboard import make_db, use_db
from test_tiers import RAW

GUARD = {"host": "127.0.0.1:6060", "x-control-action": "1"}
TAGS = {"models": [
    {"name": "qwen3.5:9b", "size": 6_600_000_000, "details": {"family": "qwen35", "parameter_size": "9B", "quantization_level": "Q4_K_M"}},
    {"name": "gemma3:12b", "size": 8_100_000_000, "details": {"family": "gemma3", "parameter_size": "12B", "quantization_level": "Q4_K_M"}},
    {"name": "bge-m3:latest", "size": 1_200_000_000, "details": {"family": "bert", "parameter_size": "567M", "quantization_level": "F16"}},
    {"junk": 1}]}


class FakeResp:
    def __init__(self, data): self._d = data
    def json(self): return self._d
    def raise_for_status(self): pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    cfg = {"tiers": {"fast": {"provider": "openrouter", "primary": "a/cheap", "fallbacks": [], "provider_policy": {"zdr": True}},
                     "local": {"provider": "ollama", "primary": "qwen3.5:9b", "fallbacks": [], "provider_policy": {"local": True}}},
           "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_KEY"},
           "ollama": {"base_url": "http://127.0.0.1:11434/v1"}}
    path = tmp_path / "router_config.json"; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config_store, "CONFIG_PATH", path)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "b"); monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr(dashboard, "CONFIG_PATH", path)
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db"); app.init_db()
    use_db(monkeypatch, tmp_path / "t.db")
    monkeypatch.setattr(app, "load_config", lambda: json.loads(path.read_text()))
    monkeypatch.setattr(local_models.httpx, "get", lambda url, **k: FakeResp(TAGS))
    monkeypatch.setitem(local_models._cache, "models", None)
    monkeypatch.setattr(catalog, "_fetch", lambda: RAW)
    monkeypatch.setitem(catalog._cache, "models", None); monkeypatch.setitem(catalog._cache, "at", 0.0)
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return TestClient(app.app), path, tmp_path


def capture(monkeypatch):
    seen = {}

    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}

    class Client:
        def __init__(self, **kw): seen["timeout"] = kw.get("timeout")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers, json): seen.update(url=url, headers=headers, body=json); return Response()

    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    return seen


def last_row(tmp_path):
    with sqlite3.connect(tmp_path / "t.db") as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1").fetchone())


def test_local_tier_calls_ollama_without_a_key_or_openrouter_fields(env, monkeypatch):
    client, _, tmp = env
    seen = capture(monkeypatch)
    r = client.post("/v1/chat/completions", json={"model": "local", "messages": [], "provider": {"order": ["x"]}, "reasoning": {"effort": "high"}})
    assert r.status_code == 200
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert "Authorization" not in seen["headers"] and "provider" not in seen["body"]
    assert seen["body"]["model"] == "qwen3.5:9b" and seen["timeout"] == 300
    row = last_row(tmp)
    assert row["estimated_cost"] == 0.0 and row["upstream_provider"] == "ollama" and row["input_tokens"] == 12


def test_a_non_loopback_ollama_url_is_refused_before_anything_is_sent(env, monkeypatch):
    client, path, _ = env
    seen = capture(monkeypatch)
    cfg = json.loads(path.read_text()); cfg["ollama"]["base_url"] = "http://gpu-box.example:11434/v1"; path.write_text(json.dumps(cfg))
    r = client.post("/v1/chat/completions", json={"model": "local", "messages": []})
    assert r.status_code == 500 and "url" not in seen


def test_openrouter_tiers_still_need_their_key_and_keep_their_policy(env, monkeypatch):
    client, _, _ = env
    seen = capture(monkeypatch)
    assert client.post("/v1/chat/completions", json={"model": "fast", "messages": []}).status_code == 503
    monkeypatch.setenv("OPENROUTER_KEY", "k")
    assert client.post("/v1/chat/completions", json={"model": "fast", "messages": []}).status_code == 200
    assert seen["headers"]["Authorization"] == "Bearer k" and seen["body"]["provider"] == {"zdr": True}


def test_local_inference_scores_as_zero_exposure_in_the_risk_model():
    assert risk.protection_of({"local": True}) == "local" and risk.EXPOSURE["local"] == 0.0
    rows = [{"agent": "a", "input_tokens": 1000, "selected_tier": "local", "timestamp": "2026-10-05T00:00:00Z"}]
    history = [{"since": "2000-01-01T00:00:00Z", "tiers": {"local": {"local": True}}}]
    out = risk.build_risk(rows, {"a": {"baseline": "restricted"}}, history)["agents"][0]
    assert out["cells"]["local"]["tokens"] == 1000 and out["risk_tokens"] == 0


def test_config_validation_accepts_ollama_tiers_and_rejects_remote_urls(env):
    _, path, _ = env
    cfg = json.loads(path.read_text())
    config_store.validate(cfg)
    cfg["ollama"]["base_url"] = "https://evil.example/v1"
    with pytest.raises(config_store.ConfigError):
        config_store.validate(cfg)
    cfg["ollama"] = "nope"
    with pytest.raises(config_store.ConfigError):
        config_store.validate(cfg)


def test_installed_models_are_listed_with_size_and_embedding_flag_last(env):
    client, _, _ = env
    d = client.get("/api/control/local-models").json()
    assert [m["id"] for m in d["models"]] == ["gemma3:12b", "qwen3.5:9b", "bge-m3:latest"]
    assert d["models"][2]["embedding"] and d["models"][0]["size_gb"] == 8.1


def test_ollama_down_or_remote_is_a_clear_503(env, monkeypatch):
    client, path, _ = env
    monkeypatch.setattr(local_models.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setitem(local_models._cache, "models", None)
    r = client.get("/api/control/local-models?refresh=true")
    assert r.status_code == 503 and "not reachable" in r.json()["status"]["error"]
    cfg = json.loads(path.read_text()); cfg["ollama"]["base_url"] = "http://10.1.1.1:11434"; path.write_text(json.dumps(cfg))
    assert "loopback" in client.get("/api/control/local-models").json()["status"]["error"]


def put(client, path, **body):
    body.setdefault("base_hash", config_store.read()[1])
    return client.put("/api/control/tiers", json=body, headers=GUARD)


def test_tiers_view_prices_local_models_at_zero_and_labels_them_local(env):
    client, _, _ = env
    t = client.get("/api/control/tiers").json()["tiers"]["local"]
    assert t["provider"] == "ollama" and t["protection"] == "local"
    assert t["chain"][0]["blended"] == 0 and t["chain"][0]["size_gb"] == 6.6 and t["chain"][0]["local"]


def test_create_a_local_tier_with_installed_models_only(env):
    client, path, _ = env
    r = put(client, path, action="create", tier="private", provider="ollama", primary="gemma3:12b", fallbacks=["qwen3.5:9b"])
    assert r.status_code == 200
    cfg = config_store.read()[0]
    assert cfg["tiers"]["private"]["provider"] == "ollama" and cfg["tiers"]["private"]["provider_policy"] == {"local": True}
    assert cfg["policy_history"][-1]["tiers"]["private"] == {"local": True}
    assert put(client, path, action="create", tier="p2", provider="ollama", primary="llama9:1b").status_code == 422
    assert "ollama pull" in put(client, path, action="create", tier="p3", provider="ollama", primary="llama9:1b").json()["message"]
    assert put(client, path, action="create", tier="p4", provider="ollama", primary="bge-m3:latest").status_code == 422
    assert put(client, path, action="create", tier="p5", provider="skynet", primary="x").status_code == 422


def test_editing_a_local_tier_keeps_it_private_whatever_the_policy_field_says(env):
    client, path, _ = env
    assert put(client, path, action="set", tier="local", primary="gemma3:12b", policy="none").status_code == 200
    assert config_store.read()[0]["tiers"]["local"]["provider_policy"] == {"local": True}
    assert put(client, path, action="set", tier="local", primary="gemma3:12b", fallbacks=["a/cheap"]).status_code == 422  # not an Ollama name that is installed


def test_the_local_privacy_level_cannot_be_put_on_a_cloud_tier(env):
    client, path, _ = env
    r = put(client, path, action="set", tier="fast", primary="a/cheap", policy="local")
    assert r.status_code == 422 and config_store.read()[0]["tiers"]["fast"]["provider_policy"] == {"zdr": True}


def test_an_unreachable_ollama_does_not_block_editing_but_models_stay_unverified(env, monkeypatch):
    client, path, _ = env
    monkeypatch.setattr(local_models.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setitem(local_models._cache, "models", None)
    assert put(client, path, action="set", tier="local", primary="whatever:7b").status_code == 200
