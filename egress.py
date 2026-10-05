"""Reader for the egress collector's files. Pure functions plus tiny file readers.

The collector (egress-collector.ps1) records which *program* connected to which *AI provider* and for
how long. It never sees content, and neither does this module. A connection being open is not proof
that data was sent, so everything here is worded as "connected", never "sent".
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "egress_log.jsonl"
IN_LOG_PATH = ROOT / "ingress_log.jsonl"
ACTIVE_PATH = ROOT / "egress_active.json"
HEARTBEAT_GRACE = 20  # seconds past the poll interval before the collector counts as stopped
ROUTER_COVERED = {"openrouter"}  # providers the router can already reach on a client's behalf
MAX_LINES = 300_000
STR_MAX = 80


# Programs the Control Center will never terminate or generate a block for.
PROTECTED = {"system", "svchost", "csrss", "lsass", "wininit", "services", "winlogon", "smss", "dwm", "explorer",
             "registry", "powershell", "pwsh", "tailscaled", "taskhostw", "sihost", "fontdrvhost", "conhost"}
# Strict whitelist: a path ends up inside a generated script, so anything unusual is refused, not escaped.
PATH_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 ._()+\-\\]{1,240}\.exe$")
RULE_PREFIX = "ModelRouter-Block-"
RULE_RE = re.compile(r"^ModelRouter-Block-[A-Za-z0-9._-]{1,120}$")


class ActionError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _parse(stamp: Any) -> datetime | None:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _s(value: Any) -> str:
    return "".join(ch for ch in str(value if value is not None else "") if ch.isprintable())[:STR_MAX]


def _clean(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    start, end = _parse(raw.get("start")), _parse(raw.get("end") or raw.get("start"))
    if not start or not end or not raw.get("provider") or not raw.get("proc"):
        return None
    return {"start": start, "end": max(start, end), "proc": _s(raw["proc"]).lower(), "provider": _s(raw["provider"]),
            "domain": _s(raw.get("domain")), "ip": _s(raw.get("ip")), "router": raw.get("router") is True,
            "shared": raw.get("shared") is True, "active": raw.get("active") is True,
            "pid": raw["pid"] if isinstance(raw.get("pid"), int) and not isinstance(raw.get("pid"), bool) and raw["pid"] > 0 else None,
            "path": str(raw["path"])[:260] if isinstance(raw.get("path"), str) else ""}


def read_log(path: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = path or LOG_PATH
    for p in (base.with_name(base.name + ".1"), base):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LINES:]
        except OSError:
            continue
        for line in lines:
            try:
                rec = _clean(json.loads(line))
            except ValueError:
                continue
            if rec:
                out.append(rec)
    return out


def read_active(path: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads((path or ACTIVE_PATH).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collector_status(active: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if not active:
        return {"state": "never", "message": "The egress collector has not written anything yet."}
    at = _parse(active.get("at"))
    poll = active.get("poll_seconds") if isinstance(active.get("poll_seconds"), int) else 3
    if not at:
        return {"state": "never", "message": "Collector heartbeat is unreadable."}
    age = (now - at).total_seconds()
    return {"state": "running" if age <= poll + HEARTBEAT_GRACE else "stopped", "last_heartbeat": active["at"],
            "age_seconds": int(age), "poll_seconds": poll,
            "domains": active.get("domains"), "ip_map": active.get("ip_map")}


def build(records: list[dict[str, Any]], active: dict[str, Any] | None, now: datetime,
          span: timedelta | None) -> dict[str, Any]:
    since = now - span if span else None
    rows = [r for r in records if since is None or r["end"] >= since]
    live = []
    for a in (active or {}).get("active") or []:
        rec = _clean({**a, "end": (active or {}).get("at"), "active": True})
        if rec:
            rec["end"] = max(rec["end"], rec["start"])
            live.append(rec)
    rows += [r for r in live if since is None or r["end"] >= since]

    providers: dict[str, dict[str, Any]] = {}
    procs: dict[str, dict[str, Any]] = {}
    for r in rows:
        secs = (r["end"] - r["start"]).total_seconds()
        for table, key in ((providers, r["provider"]), (procs, r["proc"])):
            e = table.setdefault(key, {"connections": 0, "seconds": 0.0, "via_router": 0, "direct": 0, "shared": 0,
                                       "first": r["start"], "last": r["end"], "links": {}})
            e["connections"] += 1; e["seconds"] += secs; e["shared"] += 1 if r["shared"] else 0
            e["via_router" if r["router"] else "direct"] += 1
            e["first"], e["last"] = min(e["first"], r["start"]), max(e["last"], r["end"])
            other = r["proc"] if table is providers else r["provider"]
            link = e["links"].setdefault(other, {"connections": 0, "seconds": 0.0})
            link["connections"] += 1; link["seconds"] += secs

    def fmt(table: dict[str, dict[str, Any]], label: str) -> list[dict[str, Any]]:
        out = []
        for name, e in table.items():
            out.append({label: name, "connections": e["connections"], "seconds": round(e["seconds"]),
                        "via_router": e["via_router"], "direct": e["direct"], "shared": e["shared"],
                        "first": e["first"].strftime("%Y-%m-%dT%H:%M:%SZ"), "last": e["last"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "links": sorted(({"name": k, **{"connections": v["connections"], "seconds": round(v["seconds"])}}
                                         for k, v in e["links"].items()), key=lambda x: -x["connections"])})
        return sorted(out, key=lambda x: -x["connections"])

    prov_rows, proc_rows = fmt(providers, "provider"), fmt(procs, "proc")
    # Programs reaching a provider the router could have carried: a concrete, fixable gap.
    could_route = sorted(({"proc": p["proc"], "provider": link["name"], "connections": link["connections"],
                           "shared": sum(1 for r in rows if r["proc"] == p["proc"] and r["provider"] == link["name"] and r["shared"])}
                          for p in proc_rows for link in p["links"]
                          if link["name"] in ROUTER_COVERED and p["via_router"] < p["connections"]
                          and any(r["proc"] == p["proc"] and r["provider"] == link["name"] and not r["router"] for r in rows)),
                         key=lambda x: -x["connections"])
    direct_total = sum(1 for r in rows if not r["router"])
    return {
        "totals": {"connections": len(rows), "providers": len(providers), "programs": len(procs),
                   "direct": direct_total, "via_router": len(rows) - direct_total, "shared": sum(1 for r in rows if r["shared"]),
                   "connected_seconds": round(sum((r["end"] - r["start"]).total_seconds() for r in rows))},
        "providers": prov_rows, "programs": proc_rows, "could_route": could_route,
        "active": [{"proc": r["proc"], "provider": r["provider"], "domain": r["domain"], "router": r["router"],
                    "shared": r["shared"], "pid": r["pid"], "ip": r["ip"],
                    "since": r["start"].strftime("%Y-%m-%dT%H:%M:%SZ")} for r in live],
        "blocks": [{"name": _s(b.get("name")), "enabled": b.get("enabled") is True, "program": str(b.get("program") or "")[:260],
                    "remote": [_s(a) for a in (b.get("remote") or [])[:20]]}
                   for b in (active or {}).get("blocks") or [] if isinstance(b, dict) and RULE_RE.match(str(b.get("name") or ""))],
        "unresolved": [{"proc": _s(u.get("proc")), "connections": u.get("connections") if isinstance(u.get("connections"), int) else 0,
                        "sample_ips": [_s(i) for i in (u.get("sample_ips") or [])[:3]]}
                       for u in (active or {}).get("unresolved") or [] if isinstance(u, dict)][:20],
    }


# ---------------------------------------------------------------- actions: kill and block scripts

def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:40].strip("_") or "x"


def _everything(active: dict[str, Any] | None, records: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    rows = list(records)
    for a in (active or {}).get("active") or []:
        rec = _clean({**a, "end": (active or {}).get("at"), "active": True})
        if rec:
            rows.append(rec)
    return rows


def check_kill(active: dict[str, Any] | None, pid: Any, proc: Any) -> dict[str, Any]:
    """Only a process that is holding an AI connection right now, and is not the router or a system process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ActionError("pid must be a process id")
    live = [a for a in (active or {}).get("active") or [] if isinstance(a, dict) and a.get("pid") == pid]
    if not live:
        raise ActionError("that process has no AI connection open right now (it may have already ended)", 404)
    name = str(live[0].get("proc") or "").lower()
    if not isinstance(proc, str) or proc.lower() != name:
        raise ActionError("process name does not match that pid", 409)
    if name in PROTECTED or any(x.get("router") is True for x in live) or pid in ((active or {}).get("router_pids") or []):
        raise ActionError(f"{name} is protected (the router or a system process) and will not be terminated", 403)
    return {"pid": pid, "proc": name, "connections": len(live)}


def run_kill(pid: int) -> str:
    if os.name != "nt":
        raise ActionError("terminating processes is only supported on Windows", 501)
    result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise ActionError((result.stdout + result.stderr).strip()[:200] or "taskkill failed", 502)
    return (result.stdout or "terminated").strip()[:200]


_PS_LAUNCHER = (
    "@echo off\r\n"
    "rem Model Router Control Center - generated script. Read it before you run it.\r\n"
    "net session >nul 2>&1\r\n"
    "if %errorlevel% equ 0 goto run\r\n"
    "echo Windows will ask for administrator permission to change the firewall...\r\n"
    "powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\"\r\n"
    "exit /b\r\n"
    ":run\r\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$s = Get-Content -Raw -LiteralPath '%~f0'; "
    "Invoke-Expression $s.Substring($s.LastIndexOf('::PS_BEGIN') + 10)\"\r\n"
    "echo.\r\n"
    "pause\r\n"
    "exit /b\r\n"
    "::PS_BEGIN\r\n"
)


def _wrap(ps: list[str]) -> str:
    return _PS_LAUNCHER + "\r\n".join(ps) + "\r\n"


def _valid_ips(values: list[str]) -> list[str]:
    out = []
    for v in values:
        try:
            ip = ipaddress.ip_address(v)
        except ValueError:
            continue
        if not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            out.append(str(ip))
    return sorted(set(out))[:50]


def block_script(proc: str, provider: str, scope: str, records: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
    """A self-elevating .cmd that creates one outbound Windows Firewall block rule for this program.

    scope "provider": only the addresses this program was seen using for this provider.
    scope "program": every outbound address for the program (it stops working on the network entirely).
    """
    proc, provider = (proc or "").lower(), (provider or "")
    mine = [r for r in records if r["proc"] == proc and r["provider"] == provider]
    if not mine:
        raise ActionError("no recorded connections from that program to that provider", 404)
    if proc in PROTECTED or any(r["router"] for r in records if r["proc"] == proc):
        raise ActionError(f"{proc} is protected (the router or a system process); no block script will be made", 403)
    if scope not in ("provider", "program"):
        raise ActionError("scope must be 'provider' or 'program'")
    path = next((r["path"] for r in sorted(mine, key=lambda r: r["end"], reverse=True) if r["path"]), "")
    if not path:
        raise ActionError("the program's file path was not recorded (Windows hides it for some processes)")
    if not PATH_RE.match(path):
        raise ActionError("the program's path has characters that cannot be scripted safely; block it by hand in Windows Firewall")
    ips = _valid_ips([r["ip"] for r in mine])
    if scope == "provider" and not ips:
        raise ActionError("no public addresses were recorded for that program and provider")
    name = f"{RULE_PREFIX}{_safe_part(proc)}-{_safe_part(provider)}-{'all' if scope == 'program' else 'provider'}"
    remote = "" if scope == "program" else " -RemoteAddress " + ",".join(f"'{ip}'" for ip in ips)
    ps = [
        "$ErrorActionPreference = 'Stop'",
        f"$name = '{name}'",
        "Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue",
        f"New-NetFirewallRule -DisplayName $name -Direction Outbound -Action Block -Profile Any -Program '{path}'{remote} "
        "-Description 'Created by the Model Router Control Center' | Out-Null",
        f"Write-Host ('Blocked: ' + $name)",
        "Write-Host 'New connections are blocked. Connections already open may stay up until they close; end the program to cut them now.'",
    ]
    note = {"rule": name, "program": path, "scope": scope, "addresses": ips if scope == "provider" else "all"}
    return f"block-{_safe_part(proc)}-{_safe_part(provider)}.cmd", _wrap(ps), note


def unblock_script(rule: str | None) -> tuple[str, str, dict[str, Any]]:
    if rule is None:
        ps = ["$ErrorActionPreference = 'Stop'", "Get-NetFirewallRule -DisplayName 'ModelRouter-Block-*' | Remove-NetFirewallRule",
              "Write-Host 'Removed every rule this tool created.'"]
        return "unblock-all.cmd", _wrap(ps), {"rule": "all"}
    if not isinstance(rule, str) or not RULE_RE.match(rule):
        raise ActionError("not a rule created by this tool")
    ps = ["$ErrorActionPreference = 'Stop'", f"Remove-NetFirewallRule -DisplayName '{rule}'", f"Write-Host 'Removed: {rule}'"]
    return f"unblock-{_safe_part(rule[len(RULE_PREFIX):])}.cmd", _wrap(ps), {"rule": rule}


# ---------------------------------------------------------------- inbound view

IN_KINDS = {"lan", "tailscale", "public", "loopback"}
IN_TYPES = {"remote", "router-client"}
EXPOSURE_NOTE = {"local": "this machine only", "network": "reachable from your network", "all-interfaces": "reachable from your network"}


def _clean_in(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("type") not in IN_TYPES or raw.get("kind") not in IN_KINDS:
        return None
    start, end = _parse(raw.get("start")), _parse(raw.get("end") or raw.get("start"))
    port = raw.get("port")
    if not start or not end or not raw.get("proc") or not isinstance(port, int) or isinstance(port, bool):
        return None
    return {"type": raw["type"], "start": start, "end": max(start, end), "proc": _s(raw["proc"]).lower(), "port": port,
            "remote": _s(raw.get("remote")), "kind": raw["kind"]}


def read_ingress(path: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = path or IN_LOG_PATH
    for p in (base.with_name(base.name + ".1"), base):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LINES:]
        except OSError:
            continue
        for line in lines:
            try:
                rec = _clean_in(json.loads(line))
            except ValueError:
                continue
            if rec:
                out.append(rec)
    return out


def build_inbound(records: list[dict[str, Any]], active: dict[str, Any] | None, now: datetime,
                  span: timedelta | None) -> dict[str, Any]:
    since = now - span if span else None
    rows = [r for r in records if since is None or r["end"] >= since]
    live = []
    for a in (active or {}).get("inbound") or []:
        rec = _clean_in({**a, "end": (active or {}).get("at")})
        if rec:
            live.append(rec)
    rows += [r for r in live if since is None or r["end"] >= since]

    listeners = []
    for raw in (active or {}).get("listeners") or []:
        if not isinstance(raw, dict) or raw.get("scope") not in EXPOSURE_NOTE or not isinstance(raw.get("port"), int):
            continue
        listeners.append({"proc": _s(raw.get("proc")).lower(), "port": raw["port"], "scope": raw["scope"],
                          "address": _s(raw.get("address")), "router": raw.get("router") is True,
                          "exposure": EXPOSURE_NOTE[raw["scope"]], "exposed": raw["scope"] != "local"})
    listeners.sort(key=lambda x: (not x["exposed"], x["port"], x["proc"]))

    def group(kind_type: str, key) -> list[dict[str, Any]]:
        acc: dict[Any, dict[str, Any]] = {}
        for r in rows:
            if r["type"] != kind_type:
                continue
            e = acc.setdefault(key(r), {"connections": 0, "seconds": 0.0, "first": r["start"], "last": r["end"], "kind": r["kind"]})
            e["connections"] += 1; e["seconds"] += (r["end"] - r["start"]).total_seconds()
            e["first"], e["last"] = min(e["first"], r["start"]), max(e["last"], r["end"])
        return [{"key": k, **{**v, "seconds": round(v["seconds"]), "first": v["first"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "last": v["last"].strftime("%Y-%m-%dT%H:%M:%SZ")}} for k, v in acc.items()]

    peers = sorted(({"proc": k[0], "port": k[1], "remote": k[2], **{x: v for x, v in e.items() if x != "key"}}
                    for e in group("remote", lambda r: (r["proc"], r["port"], r["remote"])) for k in [e["key"]]),
                   key=lambda x: x["last"], reverse=True)
    clients = sorted(({"proc": e["key"], **{x: v for x, v in e.items() if x != "key"}}
                      for e in group("router-client", lambda r: r["proc"])), key=lambda x: -x["connections"])
    exposed = [l for l in listeners if l["exposed"]]
    return {
        "totals": {"listeners": len(listeners), "exposed": len(exposed), "remote_peers": len({(p["remote"]) for p in peers}),
                   "public_peers": len({p["remote"] for p in peers if p["kind"] == "public"}), "router_clients": len(clients)},
        "listeners": listeners, "peers": peers[:200], "router_clients": clients,
        "router_port": (active or {}).get("router_port") if isinstance((active or {}).get("router_port"), int) else None,
    }
