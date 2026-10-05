"""Tests for the Control Center API (control.py). Reuses the dashboard fixtures, including the
sentinel `prompt_text` column that must never be served."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app
import control
import dashboard
from test_dashboard import SENTINEL, make_db, use_db


def stamp(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


ROWS = [
    {"timestamp": stamp(5), "agent": "alpha", "estimated_cost": 0.10, "latency_ms": 1000.0},
    {"timestamp": stamp(4), "agent": "alpha", "estimated_cost": 0.20, "latency_ms": 2000.0,
     "selected_tier": "balanced", "actual_model": "m2", "finish_reason": "length"},
    {"timestamp": stamp(3), "agent": None, "estimated_cost": 0.30, "latency_ms": 3000.0,
     "success": 0, "http_status": 502},
    {"timestamp": stamp(2), "agent": "benchmark-run", "estimated_cost": 5.0, "latency_ms": 500.0},
    {"timestamp": stamp(1), "agent": "alpha", "estimated_cost": 0.0, "latency_ms": 800.0,
     "output_tokens": 0, "actual_model": "m9:free", "fallback_count": 1},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    path = make_db(tmp_path / "telemetry.db", ROWS)
    # make_db stamps rows itself; overwrite with our relative timestamps
    import sqlite3
    with sqlite3.connect(path) as db:
        for i, row in enumerate(ROWS, start=1):
            db.execute("UPDATE telemetry SET timestamp=? WHERE id=?", (row["timestamp"], i))
    use_db(monkeypatch, path)
    monkeypatch.setattr(dashboard, "CONFIG_PATH", tmp_path / "router_config.json")
    (tmp_path / "router_config.json").write_text(json.dumps({"tiers": {
        "fast": {"provider": "openrouter", "primary": "m1", "fallbacks": ["m9:free"], "provider_policy": {"zdr": True}},
        "balanced": {"provider": "openrouter", "primary": "m2", "fallbacks": []}},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "SECRET_ENV_NAME"}}))
    return TestClient(app.app)


def overview(client, **params):
    r = client.get("/api/control/overview", params={"window": "1h", **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_kpis_exclude_benchmark_traffic_by_default_and_count_it_when_asked(client):
    d = overview(client)
    assert d["kpis"]["requests"] == 4 and d["synthetic_rows_hidden"] == 1
    assert d["kpis"]["cost"] == pytest.approx(0.6)
    with_synth = overview(client, include_synthetic="true")
    assert with_synth["kpis"]["requests"] == 5 and with_synth["kpis"]["cost"] == pytest.approx(5.6)


def test_untagged_calls_are_named_and_their_share_of_spend_is_reported(client):
    d = overview(client)
    assert d["kpis"]["unattributed_requests"] == 1
    assert d["kpis"]["unattributed_share"] == pytest.approx(50.0)
    names = {c["name"]: c for c in d["callers"]}
    assert names["(unattributed)"]["kind"] == "unattributed"
    assert names["alpha"]["requests"] == 3 and names["alpha"]["cost"] == pytest.approx(0.3)


def test_quality_proxies_count_truncation_empty_output_fallback_and_free_calls(client):
    k = overview(client)["kpis"]
    assert k["success_rate"] == 75.0
    assert k["truncation_rate"] == 25.0 and k["empty_rate"] == 25.0 and k["fallback_rate"] == 25.0
    assert k["free_requests"] == 1


def test_a_fallback_model_is_not_penalised_for_rescuing_a_request(client):
    models = {m["name"]: m for m in overview(client)["models"]}
    rescue = models["m9:free"]
    assert rescue["is_free"] and rescue["fallback_rate"] == 100.0
    # empty output (not the fallback) is what costs it reliability
    assert rescue["reliability"] == pytest.approx(0.0)
    assert models["m1"]["configured_in"] == ["fast"] and not models["m1"]["off_config"]


def test_tiers_carry_configured_chain_and_policy_but_never_the_credential_name(client):
    d = overview(client)
    fast = next(t for t in d["tiers"] if t["name"] == "fast")
    assert fast["primary"] == "m1" and fast["fallbacks"] == ["m9:free"] and fast["policy"] == {"zdr": True}
    assert "SECRET_ENV_NAME" not in json.dumps(d)


def test_series_is_gap_free_and_sums_to_the_window_total(client):
    d = overview(client)
    assert sum(s["requests"] for s in d["series"]) == d["kpis"]["requests"]
    assert len(d["series"]) >= 59  # one-minute buckets across an hour


def test_feed_is_newest_first_and_never_carries_content(client):
    d = overview(client)
    ids = [r["id"] for r in d["feed"]]
    assert ids == sorted(ids, reverse=True)
    assert SENTINEL not in json.dumps(d)
    assert set(d["feed"][0]) >= {"caller", "tier", "model", "latency_ms", "cost", "ok"}


def test_flow_lists_callers_tiers_models_and_declared_but_idle_routes(client):
    f = overview(client)["flow"]
    kinds = {n["kind"] for n in f["nodes"]}
    assert {"caller", "tier", "model"} <= kinds
    assert any(n["label"] == "(unattributed)" for n in f["nodes"])
    assert all(not e["to"].startswith("upstream:") for e in f["edges"])


def test_unknown_window_is_rejected_and_every_window_answers(client):
    assert client.get("/api/control/overview?window=nope").status_code == 422
    for w in control.WINDOWS:
        assert client.get("/api/control/overview", params={"window": w}).status_code == 200


def test_control_api_cannot_mutate_telemetry(client, tmp_path):
    path = tmp_path / "telemetry.db"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    overview(client); overview(client, window="all")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_database_degrades_to_an_empty_overview(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path / "absent.db")
    d = TestClient(app.app).get("/api/control/overview?window=1h").json()
    assert d["kpis"]["requests"] == 0 and d["callers"] == [] and d["feed"] == []


def test_page_is_served_and_escapes_caller_names(client):
    page = client.get("/control")
    assert page.status_code == 200 and "Control Center" in page.text
    assert "esc(r.caller)" in page.text and "esc(r.name)" in page.text  # header-supplied names are escaped


def test_classic_dashboard_and_topology_still_served(client):
    assert client.get("/dashboard").status_code == 200
    assert client.get("/architecture").status_code == 200


def test_untagged_calls_are_grouped_by_client_hint_so_you_can_name_them(client, tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        db.execute("ALTER TABLE telemetry ADD COLUMN client_hint TEXT")
        db.execute("UPDATE telemetry SET client_hint='python-httpx' WHERE agent IS NULL")
    hints = overview(client)["untagged_hints"]
    assert [h["hint"] for h in hints] == ["python-httpx"]
    assert hints[0]["requests"] == 1 and hints[0]["cost"] == pytest.approx(0.3)


def test_untagged_calls_without_a_recorded_hint_say_so(client):
    assert [h["hint"] for h in overview(client)["untagged_hints"]] == ["(not recorded)"]
