"""Retroactive privacy-exposure estimate. Pure functions, no I/O.

Telemetry never held content, so this cannot say what any call *contained*. It estimates risk
from two things the router does know:

  likelihood  how probable it is that an agent's traffic carried sensitive data. Taken from the
              agent's declared profile (what data it handles), or, when no profile exists, a rough
              proxy from tool use and context size. Marked "estimated" whenever it is a proxy.
  exposure    how weak the privacy protection was when the call went out: the tier's provider
              policy in force at that timestamp (none / no-data-collection / zero-retention).

Each matrix cell scores likelihood x exposure (0-1) and shows the tokens sent under it. The output
says "estimated exposure", never "leakage": a policy in force is not proof of how a provider behaved.
"""

from __future__ import annotations

from typing import Any

CLASSES = ("public", "internal", "sensitive", "restricted")
LIKELIHOOD = {"public": 0.10, "internal": 0.40, "sensitive": 0.80, "restricted": 1.00}
PROTECTIONS = ("none", "no-collection", "zero-retention", "local")
EXPOSURE = {"none": 1.0, "no-collection": 0.5, "zero-retention": 0.15, "local": 0.0}
INHERIT = "inherit"
BASELINES = (*CLASSES, INHERIT)

# Provider-policy history reconstructed from the router_config.json git log. A "policy_history"
# list in the config overrides this, and the config editor appends to it on every policy change.
DEFAULT_POLICY_HISTORY: list[dict[str, Any]] = [
    {"since": "2000-01-01T00:00:00Z", "note": "No privacy policy on any tier",
     "tiers": {"fast": None, "balanced": None, "deep": None, "jev": None}},
    {"since": "2026-10-04T11:18:09Z", "note": "Zero-retention floor added (fast, balanced); deep denies data collection",
     "tiers": {"fast": {"zdr": True}, "balanced": {"zdr": True},
               "deep": {"data_collection": "deny"}, "jev": None}},
    {"since": "2026-10-04T16:16:34Z", "note": "Free tier added with zero-retention floor",
     "tiers": {"fast": {"zdr": True}, "balanced": {"zdr": True}, "free": {"zdr": True},
               "deep": {"data_collection": "deny"}, "jev": None}},
]

# Starting points drafted from each agent's OpenClaw configuration. Shown as drafts to confirm,
# never applied silently.
DRAFT_PROFILES: dict[str, dict[str, Any]] = {
    "xxpress": {"baseline": "sensitive", "data": ["financial", "entity-records", "files"],
                "note": "Business operations for an LLC; full shell access"},
    "rise": {"baseline": "sensitive", "data": ["seller-account", "sales"],
             "note": "Amazon seller operations monitoring"},
    "personlab": {"baseline": "sensitive", "data": ["personal-data"],
                  "note": "Behavioral modeling research about individuals"},
    "financebot": {"baseline": "sensitive", "data": ["financial"], "note": "Draft from the name only"},
    "coordinator": {"baseline": "internal", "data": ["mixed"], "note": "Delegates to other agents; data varies"},
    "bioworks-pm": {"baseline": "internal", "data": ["client-project"], "note": "Draft from the name only"},
    "writer": {"baseline": "internal", "data": ["drafts"], "note": "Audience-aware writing"},
    "reviewer": {"baseline": "internal", "data": ["drafts"], "note": "Independent review of other agents' work"},
    "researcher": {"baseline": "public", "data": ["web-research"], "note": "Evidence-led research"},
    "openhuman": {"baseline": "internal", "data": ["personal-assistant"], "note": "General assistant"},
    "router-local": {"baseline": "internal", "data": ["mixed"], "note": "OpenClaw default agent, no per-agent tag"},
    "jev-test": {"baseline": "public", "data": ["test"], "note": "Evaluation of the jev tier"},
    "utility": {"baseline": INHERIT, "data": ["summaries"], "note": "Summarizes whatever the session holds"},
    "compaction": {"baseline": INHERIT, "data": ["summaries"], "note": "Compacts other agents' conversations"},
}


def protection_of(policy: Any) -> str:
    if not isinstance(policy, dict):
        return "none"
    if policy.get("local") is True:
        return "local"  # inference runs on this machine; nothing is sent to a provider
    if policy.get("zdr") is True:
        return "zero-retention"
    if str(policy.get("data_collection", "")).lower() == "deny":
        return "no-collection"
    return "none"


def era_index(history: list[dict[str, Any]], stamp: str | None) -> int:
    """Index of the policy era in force at ``stamp`` (ISO strings sort chronologically)."""
    chosen = 0
    for i, era in enumerate(history):
        if stamp is not None and str(era.get("since", "")) <= stamp:
            chosen = i
    return chosen


def normalise_history(history: Any) -> list[dict[str, Any]]:
    if not isinstance(history, list) or not history:
        return DEFAULT_POLICY_HISTORY
    cleaned = [e for e in history if isinstance(e, dict) and isinstance(e.get("since"), str)
               and isinstance(e.get("tiers"), dict)]
    return sorted(cleaned, key=lambda e: e["since"]) or DEFAULT_POLICY_HISTORY


def _band(score: float) -> str:
    return "high" if score >= 0.5 else "medium" if score >= 0.2 else "low"


def build_risk(rows: list[dict[str, Any]], profiles: dict[str, Any], history: Any,
               drafts: dict[str, Any] | None = None) -> dict[str, Any]:
    history = normalise_history(history)
    profiles = profiles if isinstance(profiles, dict) else {}
    drafts = DRAFT_PROFILES if drafts is None else drafts

    agents: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = (r.get("agent") or "").strip() or "(unattributed)"
        tokens = int(r.get("input_tokens") or 0)
        era = history[era_index(history, r.get("timestamp"))]
        prot = protection_of((era.get("tiers") or {}).get(r.get("selected_tier")))
        a = agents.setdefault(name, {"calls": 0, "tokens": 0, "tools_big": 0,
                                     "cells": {p: {"calls": 0, "tokens": 0} for p in PROTECTIONS}})
        a["calls"] += 1
        a["tokens"] += tokens
        if r.get("request_has_tools") and tokens >= 10000:
            a["tools_big"] += 1
        a["cells"][prot]["calls"] += 1
        a["cells"][prot]["tokens"] += tokens

    # Likelihood: declared profile when present, otherwise a proxy from tool use + context size.
    for name, a in agents.items():
        prof = profiles.get(name)
        if isinstance(prof, dict) and prof.get("baseline") in BASELINES:
            a["profile"] = {"baseline": prof["baseline"], "data": list(prof.get("data") or []),
                            "note": prof.get("note") or "", "source": "declared"}
        else:
            share = a["tools_big"] / a["calls"] if a["calls"] else 0
            a["profile"] = {"baseline": None, "data": [], "note": "", "source": "none",
                            "proxy_likelihood": round(0.25 + 0.5 * share, 2)}
    fixed = {n: (LIKELIHOOD[a["profile"]["baseline"]] if a["profile"]["baseline"] in LIKELIHOOD
                 else a["profile"].get("proxy_likelihood", 0.25))
             for n, a in agents.items() if a["profile"]["baseline"] != INHERIT}
    weight = sum(agents[n]["tokens"] for n in fixed) or 1
    inherited = sum(fixed[n] * agents[n]["tokens"] for n in fixed) / weight
    for name, a in agents.items():
        if a["profile"]["baseline"] == INHERIT:
            a["likelihood"], a["likelihood_basis"] = round(inherited, 2), "inherits the traffic it summarizes"
        elif a["profile"]["source"] == "declared":
            a["likelihood"], a["likelihood_basis"] = fixed[name], "declared profile"
        else:
            a["likelihood"], a["likelihood_basis"] = fixed[name], "estimated from tool use and context size"
        for prot, cell in a["cells"].items():
            cell["score"] = round(a["likelihood"] * EXPOSURE[prot], 3) if cell["calls"] else 0.0
            cell["band"] = _band(cell["score"]) if cell["calls"] else "none"
            cell["risk_tokens"] = round(cell["tokens"] * a["likelihood"] * EXPOSURE[prot])
        a["risk_tokens"] = sum(c["risk_tokens"] for c in a["cells"].values())
        a["draft"] = drafts.get(name)

    total_tokens = sum(a["tokens"] for a in agents.values())
    by_prot = {p: sum(a["cells"][p]["tokens"] for a in agents.values()) for p in PROTECTIONS}
    likely_sensitive_unprotected = sum(a["cells"]["none"]["tokens"] for a in agents.values() if a["likelihood"] >= 0.6)
    total_risk = sum(a["risk_tokens"] for a in agents.values())
    rows_out = [{"agent": n, **{k: v for k, v in a.items() if k not in ("tools_big",)}}
                for n, a in sorted(agents.items(), key=lambda kv: -kv[1]["risk_tokens"])]
    return {
        "eras": [{"since": e["since"], "note": e.get("note", ""),
                  "tiers": {t: protection_of(p) for t, p in (e.get("tiers") or {}).items()}} for e in history],
        "agents": rows_out,
        "totals": {"calls": sum(a["calls"] for a in agents.values()), "tokens": total_tokens,
                   "tokens_by_protection": by_prot,
                   "share_unprotected": round(by_prot["none"] * 100 / total_tokens, 1) if total_tokens else 0.0,
                   "likely_sensitive_unprotected_tokens": likely_sensitive_unprotected,
                   "risk_tokens": total_risk,
                   "unprofiled_agents": sorted(n for n, a in agents.items() if a["profile"]["source"] == "none")},
        "weights": {"likelihood": LIKELIHOOD, "exposure": EXPOSURE},
        "caveat": "Estimated exposure, not confirmed leakage: telemetry holds no content, and a policy in "
                  "force is not proof of how a provider behaved.",
    }
