"""Advisor: privacy floor, capability evidence, quality gate, cost ranking, and the API."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import advisor
import app
import canary
import catalog
import config_store
import dashboard
import local_models
from test_dashboard import make_db, use_db

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def rows(agent, tier, n=60, tin=2000, tout=300, tools=0, ok=True, cost=0.01):
    return [{"agent": agent, "selected_tier": tier, "input_tokens": tin, "output_tokens": tout,
             "request_has_tools": tools, "success": 1 if ok else 0, "estimated_cost": cost,
             "timestamp": (BASE + timedelta(hours=i * 3)).isoformat()} for i in range(n)]


TIERS = {
    "deep": {"protection": "no-collection", "provider": "openrouter", "price_in": 3.0, "price_out": 15.0},
    "balanced": {"protection": "zero-retention", "provider": "openrouter", "price_in": 0.5, "price_out": 2.0},
    "fast": {"protection": "none", "provider": "openrouter", "price_in": 0.05, "price_out": 0.2},
    "local": {"protection": "local", "provider": "ollama", "price_in": None, "price_out": None},
}


def can(tier, score, **tasks):
    return {"tier": tier, "score": score, "tasks": {k: {"score": v} for k, v in tasks.items()}}


FULL = [can("deep", 0.93, **{"needle-4k": 1.0, "needle-16k": 1.0, "tool-call": 1.0}),
        can("balanced", 0.90, **{"needle-4k": 1.0, "needle-16k": 1.0, "tool-call": 1.0}),
        can("fast", 0.60, **{"needle-4k": 1.0, "needle-16k": 0.0, "tool-call": 0.5}),
        can("local", 0.55, **{"needle-4k": 0.0, "needle-16k": 0.0, "tool-call": 1.0})]


def one(rs, profile=None, tiers=TIERS, canary_rows=FULL):
    return advisor.advise_agent("a", rs, profile, tiers, canary_rows)


def test_switches_to_the_cheapest_tier_that_passes_everything():
    out = one(rows("a", "deep"), {"baseline": "internal"})
    rec = out["recommendation"]
    assert rec["action"] == "switch" and rec["tier"] == "balanced"
    assert rec["monthly_saving"] > 0 and rec["confidence"] in ("high", "medium")


def test_privacy_floor_blocks_cheaper_but_weaker_tiers():
    out = one(rows("a", "deep"), {"baseline": "sensitive"})
    fast = next(o for o in out["options"] if o["tier"] == "fast")
    assert fast["verdict"] == "blocked" and any("protection" in r for r in fast["reasons"])
    assert out["recommendation"]["tier"] != "fast"


def test_an_agent_on_a_weak_tier_is_flagged_even_when_it_is_cheap():
    out = one(rows("a", "fast"), {"baseline": "sensitive"})
    rec = out["recommendation"]
    assert rec["action"] == "privacy" and rec["tier"] in ("balanced", "local")
    assert next(o for o in out["options"] if o["tier"] == "balanced")["verdict"] == "eligible"
    assert "balanced is protected too" in " ".join(rec["detail"])


def test_restricted_data_only_considers_local_tiers():
    out = one(rows("a", "balanced"), {"baseline": "restricted"})
    assert out["recommendation"]["action"] == "privacy"
    assert all(o["verdict"] == "blocked" for o in out["options"] if o["tier"] != "local")


def test_long_context_needs_a_passing_canary_task():
    out = one(rows("a", "deep", tin=20000), {"baseline": "public"})
    by = {o["tier"]: o for o in out["options"]}
    assert "needle-16k" in out["needs"]
    assert by["fast"]["verdict"] == "blocked" and by["local"]["verdict"] == "blocked"
    assert out["recommendation"]["tier"] == "balanced"


def test_tool_users_need_the_tool_call_task():
    out = one(rows("a", "deep", tools=1), {"baseline": "public"})
    assert "tool-call" in out["needs"]
    assert next(o for o in out["options"] if o["tier"] == "fast")["verdict"] == "blocked"


def test_quality_gate_blocks_tiers_scoring_clearly_below_the_current_one():
    out = one(rows("a", "deep", tin=500, tout=100), {"baseline": "public"})
    by = {o["tier"]: o for o in out["options"]}
    assert by["fast"]["verdict"] == "blocked" and any("below the current" in r for r in by["fast"]["reasons"])


def test_without_canary_data_it_asks_for_evidence_instead_of_guessing():
    out = one(rows("a", "deep"), {"baseline": "internal"}, canary_rows=[])
    rec = out["recommendation"]
    assert rec["action"] == "verify" and rec["confidence"] == "low" and "canary" in " ".join(rec["detail"]).lower()
    assert all(o["verdict"] != "eligible" for o in out["options"] if not o["current"])


def test_small_savings_and_free_current_tiers_are_left_alone():
    near = {**TIERS, "balanced": {**TIERS["balanced"], "price_in": 2.9, "price_out": 14.5}}
    assert one(rows("a", "deep"), {"baseline": "internal"}, tiers=near)["recommendation"]["action"] == "keep"
    free = rows("a", "local", cost=0.0)
    assert one(free, {"baseline": "internal"})["recommendation"]["action"] == "keep"


def test_local_tier_can_win_when_it_passes_what_the_agent_needs():
    canary_rows = [*FULL[:3], can("local", 0.95, **{"needle-4k": 1.0, "needle-16k": 1.0, "tool-call": 1.0})]
    out = one(rows("a", "balanced", tin=1500), {"baseline": "public"}, canary_rows=canary_rows)
    assert out["recommendation"]["tier"] == "local" and "GPU" in " ".join(out["recommendation"]["detail"])


def test_too_little_traffic_waits():
    assert one(rows("a", "deep", n=5))["recommendation"]["action"] == "wait"


def test_unprofiled_agents_are_treated_as_internal_and_say_so():
    out = one(rows("a", "deep"), None)
    assert out["profile_source"] == "assumed" and out["data_class"] == "internal"
    assert any("No data profile" in d for d in out["recommendation"]["detail"])


def test_workload_shape_and_failure_note():
    assert one(rows("a", "deep", tools=1, tin=20000))["shape"] == "tool-using agent with long context"
    assert one(rows("a", "deep", tin=300, tout=50))["shape"] == "short, small requests"
    out = one(rows("a", "deep", ok=False))
    assert any("failed" in d for d in out["recommendation"]["detail"])


def test_advise_orders_privacy_first_and_totals_savings():
    data = rows("risky", "fast") + rows("spendy", "deep")
    out = advisor.advise(data, {"risky": {"baseline": "sensitive"}, "spendy": {"baseline": "internal"}}, TIERS, FULL)
    assert [a["agent"] for a in out["agents"]][:2] == ["risky", "spendy"]
    assert out["totals"]["privacy_flags"] == 1 and out["totals"]["ready_saving_per_month"] > 0


def test_unknown_prices_never_produce_a_saving():
    tiers = {**TIERS, "balanced": {**TIERS["balanced"], "price_in": None}}
    out = one(rows("a", "deep"), {"baseline": "public"}, tiers=tiers)
    assert next(o for o in out["options"] if o["tier"] == "balanced")["cost_per_month"] is None
    assert out["recommendation"]["tier"] != "balanced"


# ---------------------------------------------------------------- API

@pytest.fixture
def api(monkeypatch, tmp_path):
    cfg = {"tiers": {"fast": {"provider": "openrouter", "primary": "a/cheap", "fallbacks": [], "provider_policy": {"zdr": True}}},
           "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "K"},
           "agent_profiles": {"bot": {"baseline": "public", "data": []}}}
    path = tmp_path / "router_config.json"; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config_store, "CONFIG_PATH", path); monkeypatch.setattr(dashboard, "CONFIG_PATH", path)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "b"); monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr(canary, "RESULTS", tmp_path / "c.jsonl")
    monkeypatch.setattr(catalog, "_fetch", lambda: [])
    monkeypatch.setitem(catalog._cache, "models", None); monkeypatch.setitem(catalog._cache, "at", 0.0)
    monkeypatch.setitem(local_models._cache, "models", None)
    make_db(tmp_path / "t.db", [])
    use_db(monkeypatch, tmp_path / "t.db")
    return TestClient(app.app)


def test_endpoint_returns_a_report_even_with_no_traffic(api):
    r = api.get("/api/control/advisor?window=7d")
    assert r.status_code == 200 and r.json()["agents"] == [] and "caveat" in r.json()


def test_endpoint_rejects_unknown_windows(api):
    assert api.get("/api/control/advisor?window=bogus").status_code == 422


def test_other_agents_routes_are_not_options():
    tiers = {**TIERS, "bot2": {**TIERS["balanced"], "agent": "bot2"}, "a": {**TIERS["deep"], "agent": "a"}}
    out = one(rows("a", "a"), {"baseline": "internal"}, tiers=tiers)
    names = [o["tier"] for o in out["options"]]
    assert "bot2" not in names and "a" in names
