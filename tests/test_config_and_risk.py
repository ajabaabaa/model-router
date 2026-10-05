"""Safe config writer, risk engine, and the guarded write endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

import app
import config_store
import control
import dashboard
import risk
from test_dashboard import make_db, use_db

BASE_CONFIG = {
    "tiers": {
        "fast": {"provider": "openrouter", "primary": "m1", "fallbacks": ["m2"], "provider_policy": {"zdr": True}},
        "balanced": {"provider": "openrouter", "primary": "m3", "fallbacks": []},
    },
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "SECRET_ENV_NAME"},
}
LOCAL = {"host": "127.0.0.1:6060"}
GUARD = {**LOCAL, "x-control-action": "1"}


@pytest.fixture
def store(monkeypatch, tmp_path):
    cfg = tmp_path / "router_config.json"
    cfg.write_text(json.dumps(BASE_CONFIG, indent=2))
    monkeypatch.setattr(config_store, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(dashboard, "CONFIG_PATH", cfg)
    return cfg


def current_hash():
    return config_store.read()[1]


# ---- config_store -------------------------------------------------------------------------

def test_update_persists_backs_up_and_audits_without_values(store):
    store.write_text(json.dumps({**BASE_CONFIG, "note": "super-secret-value"}, indent=2))
    result = config_store.update(lambda c: c["tiers"]["balanced"].update(min_max_tokens=4096),
                                 base_hash=current_hash(), action="test")
    assert result["changed"] and result["paths"] == ["tiers.balanced.min_max_tokens"]
    assert json.loads(store.read_text())["tiers"]["balanced"]["min_max_tokens"] == 4096
    backup = config_store.BACKUP_DIR / result["backup"]
    assert "min_max_tokens" not in backup.read_text()  # the backup is the *previous* file
    audit = config_store.AUDIT_PATH.read_text()
    assert "tiers.balanced.min_max_tokens" in audit and "super-secret-value" not in audit and "4096" not in audit


def test_stale_hash_is_refused_so_a_hand_edit_is_never_overwritten(store):
    stale = current_hash()
    store.write_text(json.dumps({**BASE_CONFIG, "edited_by_hand": True}))
    with pytest.raises(config_store.ConfigError) as exc:
        config_store.update(lambda c: None, base_hash=stale, action="t")
    assert exc.value.status == 409
    assert json.loads(store.read_text())["edited_by_hand"] is True
    with pytest.raises(config_store.ConfigError):
        config_store.update(lambda c: None, base_hash=None, action="t")


@pytest.mark.parametrize("mutate,fragment", [
    (lambda c: c["tiers"]["fast"].update(primary=""), "primary"),
    (lambda c: c["tiers"]["fast"].update(fallbacks=["m1"]), "twice"),
    (lambda c: c["tiers"].update({"bad#name": c["tiers"]["fast"]}), "tier name"),
    (lambda c: c["tiers"]["fast"].update(provider="anthropic"), "provider"),
    (lambda c: c["tiers"]["fast"].update(min_max_tokens=0), "min_max_tokens"),
    (lambda c: c.pop("tiers"), "tier"),
])
def test_invalid_results_are_rejected_before_touching_disk(store, mutate, fragment):
    before = store.read_text()
    with pytest.raises(config_store.ConfigError) as exc:
        config_store.update(mutate, base_hash=current_hash(), action="t")
    assert fragment in str(exc.value)
    assert store.read_text() == before and not config_store.BACKUP_DIR.exists()


def test_a_no_op_change_writes_nothing(store):
    result = config_store.update(lambda c: None, base_hash=current_hash(), action="t")
    assert result["changed"] is False and not config_store.BACKUP_DIR.exists()


def test_restore_rolls_back_and_backs_up_the_current_file_first(store):
    first = config_store.update(lambda c: c["tiers"]["fast"].update(primary="m9"), base_hash=current_hash(), action="a")
    config_store.restore(first["backup"], base_hash=first["hash"])
    assert json.loads(store.read_text())["tiers"]["fast"]["primary"] == "m1"
    assert len(config_store.list_backups()) == 2
    for bad in ("../x.json", "router_config.x/../../y.json", "other.json"):
        with pytest.raises(config_store.ConfigError):
            config_store.restore(bad, base_hash=current_hash())


def test_unparseable_config_is_reported_not_overwritten(store):
    store.write_text("{not json")
    with pytest.raises(config_store.ConfigError) as exc:
        config_store.read()
    assert exc.value.status == 409


# ---- risk engine --------------------------------------------------------------------------

def row(agent, tier, tokens, ts, tools=0):
    return {"agent": agent, "selected_tier": tier, "input_tokens": tokens, "timestamp": ts, "request_has_tools": tools}


def test_protection_levels_and_era_selection():
    assert risk.protection_of(None) == "none" and risk.protection_of({}) == "none"
    assert risk.protection_of({"zdr": True}) == "zero-retention"
    assert risk.protection_of({"data_collection": "deny"}) == "no-collection"
    hist = risk.normalise_history(None)
    assert risk.era_index(hist, "2026-10-01T00:00:00Z") == 0
    assert risk.era_index(hist, "2026-10-04T12:00:00Z") == 1
    assert risk.era_index(hist, "2026-10-04T17:00:00Z") == 2


def test_the_same_call_lands_in_a_different_column_before_and_after_the_floor():
    out = risk.build_risk([row("a", "balanced", 1000, "2026-10-01T00:00:00Z"),
                           row("a", "balanced", 2000, "2026-10-04T12:00:00Z")], {}, None, drafts={})
    cells = out["agents"][0]["cells"]
    assert cells["none"]["tokens"] == 1000 and cells["zero-retention"]["tokens"] == 2000
    assert out["totals"]["share_unprotected"] == pytest.approx(33.3, abs=0.1)


def test_declared_profile_sets_likelihood_and_unprofiled_agents_use_a_flagged_proxy():
    rows = [row("fin", "balanced", 1000, "2026-10-01T00:00:00Z"), row("anon", "balanced", 1000, "2026-10-01T00:00:00Z")]
    out = risk.build_risk(rows, {"fin": {"baseline": "sensitive", "data": ["financial"]}}, None, drafts={})
    by = {a["agent"]: a for a in out["agents"]}
    assert by["fin"]["likelihood"] == 0.8 and by["fin"]["likelihood_basis"] == "declared profile"
    assert by["anon"]["profile"]["source"] == "none" and "estimated" in by["anon"]["likelihood_basis"]
    assert out["totals"]["unprofiled_agents"] == ["anon"]
    assert by["fin"]["cells"]["none"]["band"] == "high"


def test_inherit_takes_the_token_weighted_likelihood_of_everyone_else():
    rows = [row("big", "balanced", 9000, "2026-10-01T00:00:00Z"), row("small", "balanced", 1000, "2026-10-01T00:00:00Z"),
            row("compaction", "fast", 500, "2026-10-01T00:00:00Z")]
    profiles = {"big": {"baseline": "sensitive"}, "small": {"baseline": "public"}, "compaction": {"baseline": "inherit"}}
    by = {a["agent"]: a for a in risk.build_risk(rows, profiles, None, drafts={})["agents"]}
    assert by["compaction"]["likelihood"] == pytest.approx(0.73)  # 0.9 x 0.8 + 0.1 x 0.1


def test_matrix_labels_itself_an_estimate():
    assert "not confirmed leakage" in risk.build_risk([], {}, None)["caveat"]


# ---- guarded write endpoints --------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path, store):
    use_db(monkeypatch, make_db(tmp_path / "telemetry.db", [{"agent": "alpha"}]))
    return TestClient(app.app)


def put(client, body, headers=GUARD):
    return client.put("/api/control/profiles", json=body, headers=headers)


def test_write_requires_local_host_same_origin_and_the_control_header(client):
    body = {"base_hash": current_hash(), "profiles": {"alpha": {"baseline": "internal"}}}
    assert put(client, body, {**LOCAL}).status_code == 403                                   # no header
    assert put(client, body, {**GUARD, "host": "evil.example"}).status_code == 403            # rebinding
    assert put(client, body, {**GUARD, "origin": "https://evil.example"}).status_code == 403  # cross-site
    assert put(client, body, {**GUARD, "origin": "http://127.0.0.1:6060"}).status_code == 200
    assert json.loads(config_store.CONFIG_PATH.read_text())["agent_profiles"]["alpha"]["baseline"] == "internal"


def test_profile_validation_and_stale_hash(client):
    h = current_hash()
    assert put(client, {"base_hash": h, "profiles": {"a": {"baseline": "top-secret"}}}).status_code == 422
    assert put(client, {"base_hash": h, "profiles": {"a": {"baseline": "public", "data": ["x" * 80]}}}).status_code == 422
    assert put(client, {"base_hash": "0" * 16, "profiles": {"a": {"baseline": "public"}}}).status_code == 409
    assert "agent_profiles" not in json.loads(config_store.CONFIG_PATH.read_text())


def test_saved_profile_changes_the_matrix_and_can_be_removed(client):
    d = client.get("/api/control/risk?window=all").json()
    assert "alpha" in d["totals"]["unprofiled_agents"] and d["config_hash"]
    assert put(client, {"base_hash": d["config_hash"], "profiles": {"alpha": {"baseline": "restricted", "data": ["keys"]}}}).status_code == 200
    d2 = client.get("/api/control/risk?window=all").json()
    alpha = next(a for a in d2["agents"] if a["agent"] == "alpha")
    assert alpha["profile"]["source"] == "declared" and alpha["likelihood"] == 1.0
    assert d2["audit"] and d2["audit"][0]["action"].startswith("profiles:alpha")
    assert put(client, {"base_hash": d2["config_hash"], "remove": ["alpha"]}).status_code == 200
    assert "alpha" in client.get("/api/control/risk?window=all").json()["totals"]["unprofiled_agents"]


def test_risk_view_never_serves_the_credential_name_or_content(client):
    text = json.dumps(client.get("/api/control/risk?window=all").json())
    assert "SECRET_ENV_NAME" not in text and "SENTINEL-PROMPT" not in text
