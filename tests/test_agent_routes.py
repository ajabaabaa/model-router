"""Per-agent routes: precedence in the router, config validation, create and migrate."""
import json

import pytest
from fastapi.testclient import TestClient

import app
import canary
import catalog
import config_store
import dashboard
import local_models
from test_dashboard import make_db, use_db

H = {"Host": "127.0.0.1:6060", "Origin": "http://127.0.0.1:6060", "X-Control-Action": "1"}


def test_agent_route_for_precedence():
    cfg = {"tiers": {"bot": {"agent": "bot"}, "fast": {}, "spoof": {"agent": "other"}}}
    assert app.agent_route_for(cfg, "bot") == "bot"
    assert app.agent_route_for(cfg, "fast") is None      # a plain tier is not an agent route
    assert app.agent_route_for(cfg, "spoof") is None     # agent field must match the name
    assert app.agent_route_for(cfg, None) is None


@pytest.fixture
def api(monkeypatch, tmp_path):
    t = {"provider": "openrouter", "primary": "a/m", "fallbacks": ["b/m"], "provider_policy": {"zdr": True}}
    cfg = {"tiers": {"fast": dict(t), "balanced": dict(t)},
           "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "K"}}
    path = tmp_path / "router_config.json"; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config_store, "CONFIG_PATH", path); monkeypatch.setattr(dashboard, "CONFIG_PATH", path)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "b"); monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr(canary, "RESULTS", tmp_path / "c.jsonl")
    monkeypatch.setattr(catalog, "_fetch", lambda: [{"id": "a/m", "pricing": {"prompt": "0", "completion": "0"}},
                                                    {"id": "b/m", "pricing": {"prompt": "0", "completion": "0"}}])
    monkeypatch.setitem(catalog._cache, "models", None); monkeypatch.setitem(catalog._cache, "at", 0.0)
    monkeypatch.setitem(local_models._cache, "models", None)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    mk = lambda a, tier: {"agent": a, "selected_tier": tier, "input_tokens": 1, "output_tokens": 1,
                          "request_has_tools": 0, "success": 1, "estimated_cost": 0.0, "timestamp": now}
    make_db(tmp_path / "t.db", [mk("jev", "balanced")] * 3 + [mk("bot.v2", "fast")] * 2 + [mk("bot.v2", "balanced")])
    use_db(monkeypatch, tmp_path / "t.db")
    c = TestClient(app.app); c.path = path
    return c


def h(api):
    return api.get("/api/control/tiers").json()["config_hash"]


def test_from_usage_previews_then_creates_copies_preserving_behaviour(api):
    r = api.post("/api/control/agent-routes/from-usage", headers=H, json={"base_hash": h(api)})
    assert r.status_code == 409 and {c["agent"] for c in r.json()["preview"]} == {"jev", "bot.v2"}
    r = api.post("/api/control/agent-routes/from-usage", headers=H, json={"base_hash": h(api), "confirm": True})
    assert r.status_code == 200
    tiers = json.loads(api.path.read_text())["tiers"]
    assert tiers["jev"]["agent"] == "jev" and tiers["jev"]["reasoning_effort"] == "minimal"   # from balanced
    assert tiers["bot.v2"]["primary"] == "a/m" and tiers["bot.v2"]["reasoning_effort"] == "none"  # dominant: fast
    assert "fast" in tiers and "balanced" in tiers
    again = api.post("/api/control/agent-routes/from-usage", headers=H, json={"base_hash": h(api), "confirm": True})
    assert again.status_code == 422


def test_from_usage_requires_the_write_guard(api):
    assert api.post("/api/control/agent-routes/from-usage", json={}).status_code in (403, 400)


def test_create_agent_route_and_effort(api):
    body = {"base_hash": h(api), "action": "create", "tier": "My.Agent", "agent": True, "primary": "a/m",
            "fallbacks": [], "reasoning_effort": "low"}
    assert api.put("/api/control/tiers", headers=H, json=body).status_code == 200
    t = json.loads(api.path.read_text())["tiers"]["My.Agent"]
    assert t["agent"] == "My.Agent" and t["reasoning_effort"] == "low"
    bad = {**body, "base_hash": h(api), "tier": "x", "reasoning_effort": "extreme"}
    assert api.put("/api/control/tiers", headers=H, json=bad).status_code in (409, 422)
    plain = {**body, "base_hash": h(api), "tier": "Upper", "agent": False}
    assert api.put("/api/control/tiers", headers=H, json=plain).status_code in (409, 422)


def test_route_usage_counts(api):
    r = api.get("/api/control/route-usage?window=7d").json()
    assert r["routes"]["balanced"]["calls"] == 4 and r["routes"]["fast"]["agents"] == {"bot.v2": 2}


def test_validate_rejects_mismatched_agent_and_bad_default_route():
    base = {"tiers": {"a": {"primary": "x/y", "agent": "b"}}, "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "K"}}
    with pytest.raises(config_store.ConfigError):
        config_store.validate(base)
    base["tiers"]["a"]["agent"] = "a"; base["default_route"] = "nope"
    with pytest.raises(config_store.ConfigError):
        config_store.validate(base)
