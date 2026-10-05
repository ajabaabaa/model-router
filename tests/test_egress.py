"""Egress reader: log parsing, aggregation, router vs direct, heartbeat, hostile input."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app
import egress

NOW = datetime(2026, 10, 5, 3, 0, 0, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(proc, provider, start_min, end_min, router=False, domain="api.x.com"):
    return {"v": 1, "start": iso(start_min), "end": iso(end_min), "seconds": 0, "proc": proc, "provider": provider,
            "domain": domain, "ip": "1.2.3.4", "port": 443, "router": router}


def write(tmp_path, records, extra=""):
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n" + extra, encoding="utf-8")
    return p


def test_bad_lines_and_incomplete_records_are_skipped(tmp_path):
    p = write(tmp_path, [rec("node", "openrouter", 10, 9), {"junk": 1}], extra="not json\n{\"start\": 5}\n")
    assert [r["proc"] for r in egress.read_log(p)] == ["node"]


def test_rotated_log_is_read_before_the_current_one(tmp_path):
    (tmp_path / "log.jsonl.1").write_text(json.dumps(rec("old", "openai", 100, 99)) + "\n")
    p = write(tmp_path, [rec("new", "openai", 5, 4)])
    assert [r["proc"] for r in egress.read_log(p)] == ["old", "new"]


def test_aggregation_splits_router_from_direct_and_sums_time(tmp_path):
    recs = egress.read_log(write(tmp_path, [
        rec("python", "openrouter", 30, 20, router=True), rec("python", "openrouter", 15, 14, router=True),
        rec("node", "openrouter", 12, 11), rec("chrome", "anthropic", 9, 8, domain="claude.ai")]))
    d = egress.build(recs, None, NOW, timedelta(hours=1))
    assert d["totals"] == {"connections": 4, "providers": 2, "programs": 3, "direct": 2, "via_router": 2, "shared": 0, "connected_seconds": 600 + 60 + 60 + 60}
    prov = {p["provider"]: p for p in d["providers"]}
    assert prov["openrouter"]["via_router"] == 2 and prov["openrouter"]["direct"] == 1
    assert {l["name"] for l in prov["openrouter"]["links"]} == {"python", "node"}


def test_programs_that_could_use_the_router_are_flagged_but_browsers_are_not(tmp_path):
    recs = egress.read_log(write(tmp_path, [rec("node", "openrouter", 12, 11), rec("chrome", "anthropic", 9, 8),
                                            rec("python", "openrouter", 5, 4, router=True)]))
    could = egress.build(recs, None, NOW, None)["could_route"]
    assert could == [{"proc": "node", "provider": "openrouter", "connections": 1, "shared": 0}]


def test_window_excludes_old_connections(tmp_path):
    recs = egress.read_log(write(tmp_path, [rec("a", "openai", 600, 590), rec("b", "openai", 5, 4)]))
    assert [p["proc"] for p in egress.build(recs, None, NOW, timedelta(hours=1))["programs"]] == ["b"]
    assert len(egress.build(recs, None, NOW, None)["programs"]) == 2


def test_active_connections_are_included_and_flagged_live():
    active = {"at": iso(0), "poll_seconds": 3, "active": [
        {"start": iso(2), "proc": "node", "provider": "openrouter", "domain": "openrouter.ai", "router": False}],
        "unresolved": [{"proc": "svchost", "connections": 3, "sample_ips": ["8.8.8.8"]}, "junk"]}
    d = egress.build([], active, NOW, timedelta(hours=1))
    assert d["active"][0]["proc"] == "node" and d["totals"]["connected_seconds"] == 120
    assert d["unresolved"] == [{"proc": "svchost", "connections": 3, "sample_ips": ["8.8.8.8"]}]


def test_collector_status_running_stopped_never():
    assert egress.collector_status(None, NOW)["state"] == "never"
    assert egress.collector_status({"at": iso(0), "poll_seconds": 3}, NOW)["state"] == "running"
    assert egress.collector_status({"at": iso(5), "poll_seconds": 3}, NOW)["state"] == "stopped"
    assert egress.collector_status({"at": "garbage"}, NOW)["state"] == "never"


def test_hostile_strings_are_trimmed_to_printable_and_short(tmp_path):
    r = rec("a\x07" + "b" * 500, "openai", 5, 4)
    cleaned = egress.read_log(write(tmp_path, [r]))[0]
    assert "\x07" not in cleaned["proc"] and len(cleaned["proc"]) == egress.STR_MAX


def test_endpoint_serves_the_view_and_rejects_unknown_windows(monkeypatch, tmp_path):
    log = write(tmp_path, [rec("node", "openrouter", 1, 0)])
    act = tmp_path / "active.json"; act.write_text("﻿" + json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "poll_seconds": 3, "active": []}))
    monkeypatch.setattr(egress, "LOG_PATH", log); monkeypatch.setattr(egress, "ACTIVE_PATH", act)
    c = TestClient(app.app)
    d = c.get("/api/control/egress?window=all").json()
    assert d["collector"]["state"] == "running" and d["providers"][0]["provider"] == "openrouter" and "caveat" in d
    assert c.get("/api/control/egress?window=nope").status_code == 422


def test_missing_files_degrade_to_an_empty_view(monkeypatch, tmp_path):
    monkeypatch.setattr(egress, "LOG_PATH", tmp_path / "none.jsonl"); monkeypatch.setattr(egress, "ACTIVE_PATH", tmp_path / "none.json")
    d = TestClient(app.app).get("/api/control/egress?window=24h").json()
    assert d["totals"]["connections"] == 0 and d["collector"]["state"] == "never"


def test_shared_ip_connections_are_counted_and_surfaced(tmp_path):
    a = rec("msedge", "openai", 12, 11); a["shared"] = True
    b = rec("node", "openrouter", 9, 8); b["shared"] = True
    c = rec("python", "openrouter", 5, 4, router=True)
    d = egress.build(egress.read_log(write(tmp_path, [a, b, c])), None, NOW, None)
    assert d["totals"]["shared"] == 2
    assert {p["provider"]: p["shared"] for p in d["providers"]} == {"openai": 1, "openrouter": 1}
    assert d["could_route"] == [{"proc": "node", "provider": "openrouter", "connections": 1, "shared": 1}]


# ---- kill and block controls -------------------------------------------------------------

GUARD = {"host": "127.0.0.1:6060", "x-control-action": "1"}
PATH = r"C:\Program Files\nodejs\node.exe"


def live(proc="node", pid=4242, router=False, path=PATH, ip="104.18.1.1", provider="openrouter"):
    return {"start": iso(2), "proc": proc, "provider": provider, "domain": "openrouter.ai", "ip": ip, "port": 443,
            "router": router, "pid": pid, "path": path}


def snapshot(*entries, router_pids=(9999,)):
    return {"at": iso(0), "poll_seconds": 3, "router_pids": list(router_pids), "active": list(entries), "blocks": []}


@pytest.fixture
def env(monkeypatch, tmp_path):
    import config_store
    monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(egress, "LOG_PATH", tmp_path / "none.jsonl")
    act = tmp_path / "active.json"
    monkeypatch.setattr(egress, "ACTIVE_PATH", act)
    killed = []
    monkeypatch.setattr(egress, "run_kill", lambda pid: killed.append(pid) or "SUCCESS")

    def put(snap):
        act.write_text(json.dumps(snap))
    return TestClient(app.app), put, killed


def kill(client, **body):
    return client.post("/api/control/egress/kill", json=body, headers=GUARD)


def test_kill_terminates_a_process_with_a_live_ai_connection_and_audits_it(env):
    client, put, killed = env
    put(snapshot(live()))
    r = kill(client, pid=4242, proc="Node")
    assert r.status_code == 200 and killed == [4242] and r.json()["proc"] == "node"
    import config_store
    assert "egress:kill:node:4242" in config_store.AUDIT_PATH.read_text()


@pytest.mark.parametrize("entry,pids,why", [
    (live(router=True), (1,), "router flag"),
    (live(pid=9999), (9999,), "router pid"),
    (live(proc="svchost"), (1,), "system process"),
    (live(proc="Explorer"), (1,), "system process, any case"),
])
def test_kill_refuses_the_router_and_system_processes(env, entry, pids, why):
    client, put, killed = env
    put(snapshot(entry, router_pids=pids))
    assert kill(client, pid=entry["pid"], proc=entry["proc"]).status_code == 403 and killed == [], why


def test_kill_refuses_unknown_pids_mismatched_names_and_junk(env):
    client, put, killed = env
    put(snapshot(live()))
    assert kill(client, pid=777, proc="node").status_code == 404
    assert kill(client, pid=4242, proc="chrome").status_code == 409
    assert kill(client, pid="4242", proc="node").status_code == 422
    assert kill(client, pid=1, proc="node").status_code == 422
    assert killed == []


def test_kill_needs_the_guard_headers(env):
    client, put, killed = env
    put(snapshot(live()))
    assert client.post("/api/control/egress/kill", json={"pid": 4242, "proc": "node"}, headers={"host": "127.0.0.1:6060"}).status_code == 403
    assert killed == []


def script(client, **body):
    return client.post("/api/control/egress/script", json=body, headers=GUARD)


def test_block_script_is_self_elevating_and_scoped_to_observed_addresses(env):
    client, put, _ = env
    put(snapshot(live(), live(ip="104.18.1.2", pid=4243), live(ip="10.0.0.5", pid=4244)))
    r = script(client, kind="block", proc="node", provider="openrouter", scope="provider")
    assert r.status_code == 200
    j = r.json(); text = j["script"]
    assert j["filename"] == "block-node-openrouter.cmd" and j["summary"]["addresses"] == ["104.18.1.1", "104.18.1.2"]
    assert "-Verb RunAs" in text and "goto run" in text and "LastIndexOf('::PS_BEGIN')" in text
    assert f"-Program '{PATH}'" in text and "-RemoteAddress '104.18.1.1','104.18.1.2'" in text and "10.0.0.5" not in text
    assert "New-NetFirewallRule" in text and "-Direction Outbound -Action Block" in text and "\r\n" in text


def test_program_scope_has_no_address_filter_and_says_so(env):
    client, put, _ = env
    put(snapshot(live()))
    j = script(client, kind="block", proc="node", provider="openrouter", scope="program").json()
    assert "-RemoteAddress" not in j["script"] and j["summary"]["addresses"] == "all" and j["summary"]["rule"].endswith("-all")


@pytest.mark.parametrize("path", ["C:\\x'; calc; '.exe", "C:\\a\\b$(whoami).exe", "C:\\a\\b%PATH%.exe", "C:\\a\\b\nc.exe", "\\\\server\\share\\x.exe", "C:\\a\\b.cmd", ""])
def test_hostile_or_missing_program_paths_are_refused_not_escaped(env, path):
    client, put, _ = env
    put(snapshot(live(path=path)))
    assert script(client, kind="block", proc="node", provider="openrouter", scope="provider").status_code == 422


def test_block_refuses_router_system_unknown_and_bad_scope(env):
    client, put, _ = env
    put(snapshot(live(), live(proc="python", pid=1, router=True), live(proc="svchost", pid=2)))
    assert script(client, kind="block", proc="python", provider="openrouter", scope="provider").status_code == 403
    assert script(client, kind="block", proc="svchost", provider="openrouter", scope="provider").status_code == 403
    assert script(client, kind="block", proc="ghost", provider="openrouter", scope="provider").status_code == 404
    assert script(client, kind="block", proc="node", provider="openrouter", scope="everything").status_code == 422
    assert script(client, kind="nope").status_code == 422


def test_hostile_ip_values_never_reach_the_script(env):
    client, put, _ = env
    put(snapshot(live(ip="1.2.3.4'; calc #"), live(ip="104.18.1.1", pid=5)))
    j = script(client, kind="block", proc="node", provider="openrouter", scope="provider").json()
    assert "calc" not in j["script"] and j["summary"]["addresses"] == ["104.18.1.1"]


def test_unblock_scripts_only_remove_rules_this_tool_made(env):
    client, _, _ = env
    ok = script(client, kind="unblock", rule="ModelRouter-Block-node-openrouter-provider").json()
    assert "Remove-NetFirewallRule -DisplayName 'ModelRouter-Block-node-openrouter-provider'" in ok["script"]
    assert script(client, kind="unblock", rule="Some-Other-Rule").status_code == 422
    assert script(client, kind="unblock", rule="ModelRouter-Block-x'; calc; '").status_code == 422
    assert "ModelRouter-Block-*" in script(client, kind="unblock_all").json()["script"]


def test_script_endpoint_needs_the_guard_headers_and_runs_nothing(env, monkeypatch):
    client, put, killed = env
    put(snapshot(live()))
    body = {"kind": "block", "proc": "node", "provider": "openrouter", "scope": "provider"}
    assert client.post("/api/control/egress/script", json=body, headers={"host": "127.0.0.1:6060"}).status_code == 403
    monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: pytest.fail("a script request must never execute anything"))
    assert script(client, **body).status_code == 200


def test_view_exposes_pid_active_blocks_and_ignores_foreign_rule_names(env):
    client, put, _ = env
    snap = snapshot(live()); snap["blocks"] = [{"name": "ModelRouter-Block-node-x", "enabled": True, "program": PATH, "remote": ["1.1.1.1"]},
                                               {"name": "Evil-Rule", "enabled": True}]
    put(snap)
    d = client.get("/api/control/egress?window=all").json()
    assert d["active"][0]["pid"] == 4242 and [b["name"] for b in d["blocks"]] == ["ModelRouter-Block-node-x"]


# ---- inbound view -------------------------------------------------------------------------

def irec(proc, port, remote, kind, start_min, end_min, type_="remote"):
    return {"v": 1, "type": type_, "start": iso(start_min), "end": iso(end_min), "proc": proc, "pid": 1, "port": port,
            "remote": remote, "kind": kind}


def iwrite(tmp_path, records):
    p = tmp_path / "in.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\nnot json\n")
    return p


def test_inbound_groups_peers_clients_and_flags_exposure(tmp_path):
    recs = egress.read_ingress(iwrite(tmp_path, [
        irec("tailscaled", 41641, "100.64.1.2", "tailscale", 30, 20), irec("tailscaled", 41641, "100.64.1.2", "tailscale", 10, 9),
        irec("node", 3000, "8.8.4.4", "public", 5, 4),
        irec("node", 6060, "127.0.0.1", "loopback", 30, 29, "router-client"), irec("node", 6060, "127.0.0.1", "loopback", 20, 19, "router-client"),
        irec("openhuman", 6060, "127.0.0.1", "loopback", 8, 7, "router-client"),
        {"type": "remote", "kind": "bogus", "start": iso(1), "end": iso(0), "proc": "x", "port": 1}]))
    active = {"at": iso(0), "router_port": 6060, "listeners": [
        {"proc": "python", "port": 6060, "scope": "local", "address": "127.0.0.1", "router": True},
        {"proc": "node", "port": 3000, "scope": "all-interfaces", "address": "0.0.0.0"},
        {"proc": "x", "port": 1, "scope": "weird"}, "junk"], "inbound": []}
    d = egress.build_inbound(recs, active, NOW, None)
    assert [(l["proc"], l["exposed"]) for l in d["listeners"]] == [("node", True), ("python", False)]
    assert d["totals"] == {"listeners": 2, "exposed": 1, "remote_peers": 2, "public_peers": 1, "router_clients": 2}
    assert [(c["proc"], c["connections"]) for c in d["router_clients"]] == [("node", 2), ("openhuman", 1)]
    ts = next(p for p in d["peers"] if p["proc"] == "tailscaled")
    assert ts["connections"] == 2 and ts["kind"] == "tailscale" and d["router_port"] == 6060


def test_inbound_endpoint_and_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(egress, "IN_LOG_PATH", tmp_path / "none.jsonl"); monkeypatch.setattr(egress, "ACTIVE_PATH", tmp_path / "none.json")
    c = TestClient(app.app)
    d = c.get("/api/control/inbound?window=24h").json()
    assert d["totals"]["listeners"] == 0 and d["collector"]["state"] == "never" and "caveat" in d
    assert c.get("/api/control/inbound?window=nope").status_code == 422


def test_inbound_hostile_strings_are_trimmed(tmp_path):
    r = irec("a\x07" + "b" * 400, 80, "1.2.3.4" + "9" * 300, "public", 3, 2)
    c = egress.read_ingress(iwrite(tmp_path, [r]))[0]
    assert "\x07" not in c["proc"] and len(c["proc"]) == egress.STR_MAX and len(c["remote"]) == egress.STR_MAX


def test_links_carry_the_router_split_per_program_and_provider():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    mk = lambda router: {"start": now - timedelta(minutes=5), "end": now - timedelta(minutes=4), "proc": "node", "provider": "openrouter",
                         "domain": "openrouter.ai", "ip": "1.1.1.1", "port": 443, "router": router, "shared": False, "pid": None, "active": False}
    out = egress.build([mk(True), mk(False), mk(False)], None, now, None)
    link = out["programs"][0]["links"][0]
    assert (link["via_router"], link["direct"]) == (1, 2)
