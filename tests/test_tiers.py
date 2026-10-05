"""Model & tier editor: catalog, guarded writes, safety rails, history, restore."""

import json

import pytest
from fastapi.testclient import TestClient

import app
import catalog
import config_store
import control
import dashboard
from test_dashboard import make_db, use_db

CONFIG = {
    "tiers": {
        "fast": {"provider": "openrouter", "primary": "a/cheap", "fallbacks": ["a/cheap2"], "provider_policy": {"zdr": True}},
        "deep": {"provider": "openrouter", "primary": "b/big", "fallbacks": [], "provider_policy": {"data_collection": "deny"}},
        "free": {"provider": "openrouter", "primary": "c/gift:free", "fallbacks": [], "provider_policy": {"zdr": True}},
    },
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "SECRET_ENV_NAME"},
}
RAW = [
    {"id": "a/cheap", "name": "Cheap", "context_length": 8000, "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
     "supported_parameters": ["tools"], "architecture": {"input_modalities": ["text"]}},
    {"id": "a/cheap2", "name": "Cheap Two", "context_length": 8000, "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}},
    {"id": "b/big", "name": "Big", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "b/mid", "name": "Mid", "context_length": 64000, "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
    {"id": "c/gift:free", "name": "Gift", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "c/gift2:free", "name": "Gift2", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openrouter/auto", "name": "Auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"junk": True},
]
GUARD = {"host": "127.0.0.1:6060", "x-control-action": "1"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    cfg = tmp_path / "router_config.json"
    cfg.write_text(json.dumps(CONFIG, indent=2))
    monkeypatch.setattr(config_store, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(dashboard, "CONFIG_PATH", cfg)
    use_db(monkeypatch, make_db(tmp_path / "t.db", []))
    monkeypatch.setattr(catalog, "_fetch", lambda: RAW)
    monkeypatch.setitem(catalog._cache, "models", None)
    monkeypatch.setitem(catalog._cache, "at", 0.0)
    monkeypatch.setitem(catalog._cache, "error", None)
    return TestClient(app.app), cfg


def put(client, **body):
    body.setdefault("base_hash", config_store.read()[1])
    return client.put("/api/control/tiers", json=body, headers=GUARD)


def cfg_now():
    return config_store.read()[0]


def test_catalog_normalises_prices_flags_free_and_skips_junk(env):
    client, _ = env
    models = {m["id"]: m for m in client.get("/api/control/catalog?limit=100").json()["models"]}
    assert "junk" not in models and len(models) == 7
    assert models["a/cheap"]["in"] == 0.1 and models["a/cheap"]["out"] == 0.4 and models["a/cheap"]["tools"]
    assert models["c/gift:free"]["free"] and not models["b/big"]["free"]
    assert models["openrouter/auto"]["in"] is None  # variable pricing is not a number


def test_catalog_search_filters_and_sorts_cheapest_first(env):
    client, _ = env
    assert [m["id"] for m in client.get("/api/control/catalog?q=b/").json()["models"]] == ["b/mid", "b/big"]
    assert {m["id"] for m in client.get("/api/control/catalog?free=true").json()["models"]} == {"c/gift:free", "c/gift2:free"}
    assert [m["id"] for m in client.get("/api/control/catalog?tools=true").json()["models"]] == ["a/cheap"]


def test_catalog_failure_serves_stale_copy_or_503(env, monkeypatch):
    client, _ = env
    assert client.get("/api/control/catalog").status_code == 200
    monkeypatch.setattr(catalog, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    r = client.get("/api/control/catalog?refresh=true")
    assert r.status_code == 200 and r.json()["status"]["stale"]
    monkeypatch.setitem(catalog._cache, "models", None)
    assert client.get("/api/control/catalog?refresh=true").status_code == 503


def test_tiers_view_has_prices_blended_at_your_ratio_and_never_the_key_name(env):
    client, _ = env
    d = client.get("/api/control/tiers").json()
    fast = d["tiers"]["fast"]
    assert fast["protection"] == "zero-retention" and fast["chain"][0]["known"]
    assert fast["chain"][0]["blended"] == pytest.approx((72 * 0.1 + 0.4) / 73, abs=1e-3)
    assert d["tiers"]["deep"]["protection"] == "no-collection"
    assert "SECRET_ENV_NAME" not in json.dumps(d)


def test_writes_need_the_guard_headers(env):
    client, _ = env
    body = {"base_hash": config_store.read()[1], "action": "set", "tier": "fast", "primary": "a/cheap"}
    assert client.put("/api/control/tiers", json=body, headers={"host": "127.0.0.1:6060"}).status_code == 403
    assert client.put("/api/control/tiers", json=body, headers={**GUARD, "host": "evil.example"}).status_code == 403


def test_set_chain_persists_with_backup_and_keeps_other_policy_keys(env):
    client, cfg = env
    raw = json.loads(cfg.read_text()); raw["tiers"]["fast"]["provider_policy"]["order"] = ["x"]
    cfg.write_text(json.dumps(raw))
    r = put(client, action="set", tier="fast", primary="b/mid", fallbacks=["a/cheap"])
    assert r.status_code == 200 and r.json()["changed"] and r.json()["backup"]
    fast = cfg_now()["tiers"]["fast"]
    assert fast["primary"] == "b/mid" and fast["fallbacks"] == ["a/cheap"]
    assert fast["provider_policy"] == {"zdr": True, "order": ["x"]}


def test_unknown_model_is_rejected_but_models_already_in_the_tier_are_kept(env):
    client, cfg = env
    assert put(client, action="set", tier="fast", primary="z/missing").status_code == 422
    raw = json.loads(cfg.read_text()); raw["tiers"]["fast"]["primary"] = "custom/local"; cfg.write_text(json.dumps(raw))
    assert put(client, action="set", tier="fast", primary="custom/local", fallbacks=["a/cheap"]).status_code == 200
    assert put(client, action="set", tier="fast", primary="not an id").status_code == 422
    assert put(client, action="set", tier="fast", primary="a/cheap", fallbacks=["a/cheap"]).status_code == 422  # duplicate


def test_free_tier_only_takes_free_models_and_chain_length_is_capped(env):
    client, _ = env
    assert put(client, action="set", tier="free", primary="b/big").status_code == 422
    assert put(client, action="set", tier="free", primary="c/gift2:free").status_code == 200
    ids = ["a/cheap", "a/cheap2", "b/big", "b/mid", "c/gift:free", "c/gift2:free", "openrouter/auto"]
    assert put(client, action="set", tier="fast", primary=ids[0], fallbacks=ids[1:]).status_code == 422


def test_lowering_privacy_needs_confirmation_and_is_recorded_in_policy_history(env):
    client, _ = env
    r = put(client, action="set", tier="fast", primary="a/cheap", policy="none")
    assert r.status_code == 409 and r.json()["error"] == "needs_confirmation" and "fast" in r.json()["message"]
    assert cfg_now()["tiers"]["fast"]["provider_policy"] == {"zdr": True}  # nothing written
    assert put(client, action="set", tier="fast", primary="a/cheap", policy="none", confirm=True).status_code == 200
    cfg = cfg_now()
    assert "provider_policy" not in cfg["tiers"]["fast"]
    last = cfg["policy_history"][-1]
    assert last["tiers"]["fast"] is None and last["tiers"]["deep"] == {"data_collection": "deny"}
    assert len(cfg["policy_history"]) == 4  # three default eras + this one


def test_raising_privacy_needs_no_confirmation(env):
    client, cfg = env
    assert put(client, action="set", tier="deep", primary="b/big", policy="zdr").status_code == 200
    assert cfg_now()["tiers"]["deep"]["provider_policy"] == {"zdr": True}


def test_create_and_delete_tiers_with_rails(env):
    client, _ = env
    assert put(client, action="create", tier="Bad Name", primary="a/cheap").status_code == 422
    assert put(client, action="create", tier="fast", primary="a/cheap").status_code == 409
    r = put(client, action="create", tier="research", primary="b/mid", fallbacks=["a/cheap"])
    assert r.status_code == 200
    assert cfg_now()["tiers"]["research"]["provider_policy"] == {"zdr": True}  # private by default
    assert put(client, action="delete", tier="research").status_code == 409  # asks first
    assert put(client, action="delete", tier="research", confirm=True).status_code == 200
    assert "research" not in cfg_now()["tiers"]
    assert put(client, action="delete", tier="ghost", confirm=True).status_code == 404


def test_cannot_delete_last_tier_or_one_the_config_still_names(env):
    client, cfg = env
    raw = json.loads(cfg.read_text()); raw["default_tier"] = "deep"; cfg.write_text(json.dumps(raw))
    assert put(client, action="delete", tier="deep", confirm=True).status_code == 422
    raw = {"tiers": {"only": CONFIG["tiers"]["fast"]}, "openrouter": CONFIG["openrouter"]}
    cfg.write_text(json.dumps(raw))
    assert put(client, action="delete", tier="only", confirm=True).status_code == 422


def test_stale_hash_is_refused(env):
    client, cfg = env
    stale = config_store.read()[1]
    cfg.write_text(json.dumps({**CONFIG, "edited": True}))
    assert put(client, base_hash=stale, action="set", tier="fast", primary="a/cheap").status_code == 409


def test_restore_undoes_a_change_and_backs_up_first(env):
    client, _ = env
    before = cfg_now()["tiers"]["fast"]["primary"]
    name = put(client, action="set", tier="fast", primary="b/mid").json()["backup"]
    r = client.post("/api/control/restore", json={"name": name, "base_hash": config_store.read()[1]}, headers=GUARD)
    assert r.status_code == 200 and cfg_now()["tiers"]["fast"]["primary"] == before
    assert client.post("/api/control/restore", json={"name": "../x.json", "base_hash": "x"}, headers=GUARD).status_code == 422
