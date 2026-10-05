"""Advisor: per-agent model recommendations from what the router has actually seen.

Pure functions, no I/O, so every rule is testable. Inputs are telemetry rows (no content), the
declared agent profiles, the configured tiers with their prices and protection, and the latest canary
scores. For each real caller it asks four questions:

  1. Privacy: which tiers give this agent's data enough protection? (blocked tiers are never suggested)
  2. Capability: does the agent need long context or tool calls, and did the tier pass those canary tasks?
  3. Quality: is the tier's canary score at least as good as the one the agent uses now?
  4. Cost: of the tiers that pass, which is cheapest for this agent's real token sizes?

It only recommends a switch when the evidence supports it. Missing canary data produces a "verify
first" note instead of a guess, and a recommendation never overrides the privacy floor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PROTECTION_RANK = {"none": 0, "no-collection": 1, "zero-retention": 2, "local": 3}
# Minimum protection by data class. "internal" accepts no-collection; sensitive wants zero retention.
MIN_RANK = {"public": 0, "internal": 1, "sensitive": 2, "restricted": 3}
DEFAULT_CLASS = "internal"
MIN_CALLS = 20
SAVING_THRESHOLD = 0.10          # ignore savings under 10%: not worth a routing change
QUALITY_TOLERANCE = 0.075        # about one task out of fourteen
LONG_CONTEXT = {"needle-4k": 3000, "needle-16k": 12000}
TOOLS_SHARE = 0.2


def _stamp(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def shape_of(f: dict[str, Any]) -> str:
    if f["tools_share"] >= TOOLS_SHARE and f["p90_in"] >= LONG_CONTEXT["needle-16k"]:
        return "tool-using agent with long context"
    if f["tools_share"] >= TOOLS_SHARE:
        return "tool-using agent"
    if f["p90_in"] >= LONG_CONTEXT["needle-16k"]:
        return "long-context work"
    if f["avg_out"] and f["avg_out"] < 250 and f["avg_in"] < 2500:
        return "short, small requests"
    return "general chat and writing"


def features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ins = [_num(r.get("input_tokens")) for r in rows]
    outs = [_num(r.get("output_tokens")) for r in rows]
    stamps = [s for s in (_stamp(r.get("timestamp")) for r in rows) if s]
    span_days = max(1.0, (max(stamps) - min(stamps)).total_seconds() / 86400) if len(stamps) > 1 else 1.0
    mix: dict[str, int] = {}
    cost: dict[str, float] = {}
    for r in rows:
        t = r.get("selected_tier") or "?"
        mix[t] = mix.get(t, 0) + 1
        cost[t] = cost.get(t, 0.0) + _num(r.get("estimated_cost"))
    f = {"calls": n, "per_day": round(n / span_days, 1), "avg_in": round(sum(ins) / n) if n else 0,
         "p90_in": round(_p90(ins)), "avg_out": round(sum(outs) / n) if n else 0,
         "tools_share": round(sum(1 for r in rows if r.get("request_has_tools")) / n, 2) if n else 0.0,
         "failure_rate": round(sum(1 for r in rows if not r.get("success")) / n, 3) if n else 0.0,
         "tier_mix": {t: round(c / n, 2) for t, c in sorted(mix.items(), key=lambda kv: -kv[1])},
         "observed_cost": round(sum(cost.values()), 4)}
    f["shape"] = shape_of(f)
    f["dominant_tier"] = max(mix, key=mix.get) if mix else None
    return f


def needs_of(f: dict[str, Any]) -> list[str]:
    need = []
    for task, size in sorted(LONG_CONTEXT.items(), key=lambda kv: kv[1], reverse=True):
        if f["p90_in"] >= size:
            need.append(task)
            break
    if f["tools_share"] >= TOOLS_SHARE:
        need.append("tool-call")
    return need


def _canary_index(canary: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {t["tier"]: t for t in canary or []}


def estimate(tier: dict[str, Any], f: dict[str, Any]) -> float | None:
    """Per-call cost at this agent's average sizes. Local tiers are free; unknown prices stay unknown."""
    if tier.get("protection") == "local" or tier.get("provider") == "ollama":
        return 0.0
    pin, pout = tier.get("price_in"), tier.get("price_out")
    if pin is None or pout is None:
        return None
    return (f["avg_in"] * pin + f["avg_out"] * pout) / 1_000_000


def _option(name: str, tier: dict[str, Any], f: dict[str, Any], need: list[str], min_rank: int,
            canary: dict[str, Any] | None, current_score: float | None, is_current: bool) -> dict[str, Any]:
    reasons: list[str] = []
    verdict = "eligible"
    rank = PROTECTION_RANK.get(tier.get("protection"), 0)
    if rank < min_rank:
        verdict = "blocked"
        reasons.append(f"protection '{tier.get('protection')}' is below what this data needs")
    results = (canary or {}).get("tasks") or {}
    for task in need:
        got = results.get(task)
        if got is None:
            if verdict != "blocked":
                verdict = "unverified"
            reasons.append(f"no canary result for {task}")
        elif got["score"] < 0.99:
            verdict = "blocked" if verdict != "blocked" else verdict
            reasons.append(f"failed {task} in the canary ({got['score']:.2f})")
    score = (canary or {}).get("score")
    if canary is None and verdict == "eligible":
        verdict = "unverified"
        reasons.append("canary not run for this tier")
    if verdict == "eligible" and not is_current:
        if current_score is None:
            verdict = "unverified"
            reasons.append("the current tier has no canary score to compare with")
        elif score is not None and score < current_score - QUALITY_TOLERANCE:
            verdict = "blocked"
            reasons.append(f"canary score {score:.2f} is below the current tier's {current_score:.2f}")
    per_call = estimate(tier, f)
    return {"tier": name, "protection": tier.get("protection"), "provider": tier.get("provider"),
            "canary_score": score, "cost_per_call": per_call,
            "cost_per_month": None if per_call is None else round(per_call * f["per_day"] * 30, 2),
            "verdict": verdict, "reasons": reasons, "current": is_current}


def advise_agent(name: str, rows: list[dict[str, Any]], profile: Any, tiers: dict[str, dict[str, Any]],
                 canary: list[dict[str, Any]]) -> dict[str, Any]:
    f = features(rows)
    declared = isinstance(profile, dict) and profile.get("baseline") in MIN_RANK
    klass = profile["baseline"] if declared else DEFAULT_CLASS
    min_rank = MIN_RANK[klass]
    need = needs_of(f)
    idx = _canary_index(canary)
    current = f["dominant_tier"]
    cur_canary = idx.get(current)
    current_score = cur_canary["score"] if cur_canary else None
    out: dict[str, Any] = {"agent": name, **f, "data_class": klass,
                           "profile_source": "declared" if declared else "assumed",
                           "min_protection": next(p for p, r in PROTECTION_RANK.items() if r == min_rank),
                           "needs": need}
    if f["calls"] < MIN_CALLS:
        out.update(options=[], recommendation={"action": "wait", "tier": None, "confidence": "low",
                   "headline": "Not enough traffic yet", "detail": [f"{f['calls']} calls seen; at least {MIN_CALLS} are needed for a recommendation."],
                   "monthly_saving": None})
        return out
    options = [_option(n, t, f, need, min_rank, idx.get(n), current_score, n == current)
               for n, t in tiers.items()]
    out["options"] = sorted(options, key=lambda o: (o["cost_per_call"] is None, o["cost_per_call"] or 0))
    cur = next((o for o in options if o["current"]), None)
    detail: list[str] = []
    rec: dict[str, Any] = {"action": "keep", "tier": current, "monthly_saving": None, "detail": detail}
    cur_cost = cur["cost_per_month"] if cur else None
    blocked_now = bool(cur and cur["verdict"] == "blocked" and any("protection" in r for r in cur["reasons"]))
    if not declared:
        detail.append("No data profile is declared for this agent, so it is treated as 'internal'. Set one in Risk to sharpen this.")
    if need:
        detail.append("Needs: " + ", ".join(need) + f" (p90 prompt {f['p90_in']:,} tokens, {int(f['tools_share'] * 100)}% tool calls).")

    safe = [o for o in options if o["verdict"] == "eligible" and not o["current"] and o["cost_per_call"] is not None]
    safe.sort(key=lambda o: o["cost_per_call"])
    pending = [o for o in options if o["verdict"] == "unverified" and not o["current"] and o["cost_per_call"] is not None
               and PROTECTION_RANK.get(o["protection"], 0) >= min_rank]
    pending.sort(key=lambda o: o["cost_per_call"])

    def saves(o: dict[str, Any]) -> float:
        return (cur_cost - o["cost_per_month"]) if cur_cost is not None and o["cost_per_month"] is not None else 0.0

    def worthwhile(o: dict[str, Any]) -> bool:
        return cur_cost is not None and cur_cost > 0 and saves(o) / cur_cost >= SAVING_THRESHOLD

    if blocked_now:
        rec.update(action="privacy", headline=f"{current} gives less protection than this data warrants",
                   tier=safe[0]["tier"] if safe else None)
        detail.append(f"This agent works with {klass} data but mostly runs on a tier with '{cur['protection']}' protection.")
        if safe:
            detail.append(f"{safe[0]['tier']} has enough protection and passes the checks that apply (canary {safe[0]['canary_score']:.2f}"
                          + (f" vs {current_score:.2f} now" if current_score is not None else "") + ").")
            better = max(safe[1:], key=lambda o: o["canary_score"] or 0, default=None)
            if better and (better["canary_score"] or 0) > (safe[0]["canary_score"] or 0) + QUALITY_TOLERANCE:
                detail.append(f"If quality matters more than cost, {better['tier']} is protected too and scored {better['canary_score']:.2f}.")
        else:
            detail.append("No other tier currently has enough protection and passing evidence; a zero-retention or local tier is needed.")
        rec["confidence"] = "medium"
    elif safe and worthwhile(safe[0]):
        best = safe[0]
        rec.update(action="switch", tier=best["tier"], monthly_saving=round(saves(best), 2),
                   headline=f"Move to {best['tier']} to save about ${saves(best):.2f}/month")
        detail.append(f"{best['tier']} passed every check that applies here" +
                      (f" and scored {best['canary_score']:.2f} against {current_score:.2f} for {current}." if best["canary_score"] is not None and current_score is not None else "."))
        if best["provider"] == "ollama":
            detail.append("It runs on your own GPU: free, but slower and limited by context size. Check that the Ollama copy has a large enough context window.")
        rec["confidence"] = "high" if f["calls"] >= 100 and best["canary_score"] is not None and current_score is not None else "medium"
    elif pending and worthwhile(pending[0]):
        best = pending[0]
        rec.update(action="verify", tier=best["tier"], monthly_saving=round(saves(best), 2),
                   headline=f"Test {best['tier']}: it could save about ${saves(best):.2f}/month",
                   confidence="low")
        detail.append(f"Not recommended yet: {'; '.join(best['reasons'])}. Run the canary on {best['tier']}"
                      + (f" and on {current}" if current_score is None else "") + " to find out.")
    else:
        rec.update(action="keep", headline=f"Keep {current}", confidence="medium" if current_score is not None else "low")
        if cur_cost == 0 or cur_cost is None:
            detail.append("Already free or price unknown, so there is no saving to chase." if cur_cost == 0 else "The current tier's price is unknown, so savings cannot be estimated.")
        else:
            detail.append("No cheaper tier passes the privacy and capability checks by a meaningful margin.")
        if current_score is None:
            detail.append("The canary has not run on this tier, so quality is unmeasured.")
    if f["failure_rate"] >= 0.1:
        detail.append(f"{int(f['failure_rate'] * 100)}% of its calls failed in this window; look at reliability before cost.")
    out["recommendation"] = rec
    out["current_cost_per_month"] = cur_cost
    return out


def advise(rows: list[dict[str, Any]], profiles: dict[str, Any], tiers: dict[str, dict[str, Any]],
           canary: list[dict[str, Any]]) -> dict[str, Any]:
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_agent.setdefault((r.get("agent") or "").strip() or "(unattributed)", []).append(r)
    profiles = profiles if isinstance(profiles, dict) else {}
    agents = [advise_agent(n, rs, profiles.get(n), tiers, canary) for n, rs in by_agent.items()]
    order = {"privacy": 0, "switch": 1, "verify": 2, "keep": 3, "wait": 4}
    agents.sort(key=lambda a: (order[a["recommendation"]["action"]], -(a["recommendation"].get("monthly_saving") or 0), -a["calls"]))
    total = sum(a["recommendation"]["monthly_saving"] or 0 for a in agents if a["recommendation"]["action"] == "switch")
    potential = sum(a["recommendation"]["monthly_saving"] or 0 for a in agents if a["recommendation"]["action"] == "verify")
    return {"agents": agents,
            "totals": {"agents": len(agents), "ready_saving_per_month": round(total, 2),
                       "unverified_saving_per_month": round(potential, 2),
                       "privacy_flags": sum(1 for a in agents if a["recommendation"]["action"] == "privacy"),
                       "tiers_with_canary": sorted(t["tier"] for t in canary or [])},
            "caveat": "Estimates use each agent's average token sizes and list prices, ignore prompt caching, and assume "
                      "the canary suite represents the work. Treat savings as a ranking signal, then test on real work "
                      "before switching an important agent."}
