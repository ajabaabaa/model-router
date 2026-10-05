"""Program -> agent -> route -> model map for the Control Center, built from telemetry rows plus config.

Programs are the software that calls the router (OpenClaw, OpenHuman ...). A route can carry an
optional ``program`` label; a few name prefixes are recognised; everything else is "Unassigned" so
the page asks the operator rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any

import risk

UNASSIGNED = "Unassigned"
UNATTRIBUTED = "(unattributed)"
PROGRAM_PREFIX = (("openhuman", "OpenHuman"), ("router-", "Router tests"), ("jev", "jev"))
PROGRAM_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,29}$")


def program_of(agent: str, tiers: dict[str, Any]) -> str:
    route = tiers.get(agent)
    if isinstance(route, dict) and isinstance(route.get("program"), str) and route["program"]:
        return route["program"]
    low = agent.lower()
    for prefix, name in PROGRAM_PREFIX:
        if low.startswith(prefix):
            return name
    return UNASSIGNED


def _prot(tier: Any) -> str:
    if not isinstance(tier, dict):
        return "none"
    return "local" if tier.get("provider") == "ollama" else risk.protection_of(tier.get("provider_policy"))


def build(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    tiers = config.get("tiers") if isinstance(config.get("tiers"), dict) else {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    def node(nid: str, col: int, label: str, **meta: Any) -> dict[str, Any]:
        n = nodes.setdefault(nid, {"id": nid, "col": col, "label": label, "requests": 0, "cost": 0.0,
                                   "failures": 0, "fallbacks": 0})
        n.update({k: v for k, v in meta.items() if v is not None})
        return n

    def edge(a: str, b: str) -> dict[str, Any]:
        return edges.setdefault((a, b), {"from": a, "to": b, "requests": 0, "cost": 0.0, "failures": 0, "fallbacks": 0})

    for row in rows:
        agent = (row.get("agent") or "").strip() or UNATTRIBUTED
        route = row.get("selected_tier") or "(none)"
        model = row.get("actual_model") or "(none)"
        program = program_of(agent, tiers) if agent != UNATTRIBUTED else UNASSIGNED
        cost = float(row.get("estimated_cost") or 0)
        failed = 0 if str(row.get("success")).lower() in ("1", "true") else 1
        fb = 1 if int(row.get("fallback_count") or 0) else 0
        owned = isinstance(tiers.get(route), dict) and tiers[route].get("agent") == route
        ids = (f"p:{program}", f"a:{agent}", f"r:{route}", f"m:{model}")
        node(ids[0], 0, program)
        node(ids[1], 1, agent)
        node(ids[2], 2, route, protection=_prot(tiers.get(route)), shared=not owned,
             local=isinstance(tiers.get(route), dict) and tiers[route].get("provider") == "ollama",
             missing=route not in tiers)
        node(ids[3], 3, model)
        for nid in ids:
            n = nodes[nid]; n["requests"] += 1; n["cost"] += cost; n["failures"] += failed; n["fallbacks"] += fb
        for a, b in zip(ids, ids[1:]):
            e = edge(a, b); e["requests"] += 1; e["cost"] += cost; e["failures"] += failed; e["fallbacks"] += fb

    # idle agent routes still appear, so a freshly created route is visible before it has traffic
    for name, tier in tiers.items():
        if isinstance(tier, dict) and tier.get("agent") == name and f"r:{name}" not in nodes:
            program = program_of(name, tiers)
            node(f"p:{program}", 0, program); node(f"a:{name}", 1, name)
            node(f"r:{name}", 2, name, protection=_prot(tier), shared=False, local=tier.get("provider") == "ollama", missing=False)
            edge(f"p:{program}", f"a:{name}"); edge(f"a:{name}", f"r:{name}")
            primary = tier.get("primary")
            if isinstance(primary, str) and primary:
                node(f"m:{primary}", 3, primary); edge(f"r:{name}", f"m:{primary}")
    for n in nodes.values():
        n["cost"] = round(n["cost"], 6)
    for e in edges.values():
        e["cost"] = round(e["cost"], 6)
    return {"nodes": list(nodes.values()), "edges": list(edges.values()),
            "columns": ["Programs", "Agents", "Routes", "Models"]}
