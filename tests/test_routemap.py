import routemap

CFG = {"tiers": {
    "writer": {"agent": "writer", "program": "OpenClaw", "primary": "a/m", "provider_policy": {"zdr": True}},
    "openhuman": {"agent": "openhuman", "primary": "a/m", "provider_policy": {"data_collection": "deny"}},
    "idle-bot": {"agent": "idle-bot", "primary": "z/z"},
    "balanced": {"primary": "a/m", "provider_policy": {"zdr": True}},
    "local": {"provider": "ollama", "primary": "q"}}}


def row(agent, tier, model="a/m", ok=1, fb=0, cost=0.01):
    return {"agent": agent, "selected_tier": tier, "actual_model": model, "success": ok, "fallback_count": fb, "estimated_cost": cost}


def test_columns_edges_and_programs():
    m = routemap.build([row("writer", "writer"), row("writer", "writer", ok=0), row("openhuman", "balanced"), row("", "balanced")], CFG)
    by = {n["id"]: n for n in m["nodes"]}
    assert by["a:writer"]["col"] == 1 and by["p:OpenClaw"]["requests"] == 2
    assert by["p:OpenHuman"]["requests"] == 1 and by["p:Unassigned"]["requests"] == 1   # prefix rule + unattributed
    assert by["r:writer"]["protection"] == "zero-retention" and by["r:writer"]["shared"] is False
    assert by["r:balanced"]["shared"] is True
    e = next(e for e in m["edges"] if (e["from"], e["to"]) == ("a:writer", "r:writer"))
    assert e["requests"] == 2 and e["failures"] == 1


def test_idle_agent_routes_and_missing_routes_show():
    m = routemap.build([row("x", "ghost")], CFG)
    by = {n["id"]: n for n in m["nodes"]}
    assert "a:idle-bot" in by and by["m:z/z"]["requests"] == 0
    assert by["r:ghost"]["missing"] is True
    assert routemap.build([row("a", "local", model="q")], CFG)["nodes"][2]["local"] is True
