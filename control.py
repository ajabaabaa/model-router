"""Control Center API for Model Router (read-only, phase 1).

One endpoint feeds the whole /control page so every number on screen comes from the same
row set and the same instant:

  GET /api/control/overview?window=1h|6h|24h|7d|all&include_synthetic=false
  GET /control                      (the page)

Same rules as dashboard.py: telemetry is opened read-only, only whitelisted operational
columns are selected (no prompt or response content exists in the table, and none could be
read even if a column were added), and a fault here returns 503 without touching routing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

import canary
import catalog
import config_store
import copy
import dashboard
import egress
import local_models
import re
import risk
from dashboard import _agent_kind, _parse_timestamp, build_topology, load_router_config
from telemetry_report import _is_true, _percentile, connect_read_only

PAGE_PATH = Path(__file__).resolve().parent / "static" / "control.html"

COLUMNS = (
    "id", "timestamp", "requested_tier", "selected_tier", "routing_automatic", "routing_reason",
    "actual_model", "input_tokens", "output_tokens", "latency_ms", "estimated_cost",
    "http_status", "success", "fallback_count", "budget_exhausted", "finish_reason",
    "agent", "client_hint", "request_has_tools", "request_has_stream", "attempt_outcomes",
)

# window -> (span, bucket width). Bucket widths keep every chart at roughly 30-60 points.
WINDOWS: dict[str, tuple[timedelta | None, timedelta]] = {
    "15m": (timedelta(minutes=15), timedelta(seconds=30)),
    "1h": (timedelta(hours=1), timedelta(minutes=1)),
    "6h": (timedelta(hours=6), timedelta(minutes=5)),
    "24h": (timedelta(hours=24), timedelta(minutes=30)),
    "7d": (timedelta(days=7), timedelta(hours=4)),
    "all": (None, timedelta(days=1)),
}

FEED_ROWS = 80
UNATTRIBUTED = "(unattributed)"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(since: datetime | None, until: datetime | None = None) -> list[dict[str, Any]]:
    path = dashboard.db_path()
    if not path.exists():
        return []
    where, args = [], []
    if since is not None:
        where.append("timestamp >= ?"); args.append(_iso(since))
    if until is not None:
        where.append("timestamp < ?"); args.append(_iso(until))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with connect_read_only(path) as db:
        if not dashboard._has_telemetry_table(db):
            return []
        present = {r[1] for r in db.execute("PRAGMA table_info(telemetry)")}
        cols = [c for c in COLUMNS if c in present]
        rows = db.execute(f"SELECT {', '.join(cols)} FROM telemetry{clause} ORDER BY id", args).fetchall()
    return [dict(r) for r in rows]


def _is_synthetic(row: dict[str, Any]) -> bool:
    tier = str(row.get("selected_tier") or "")
    return _agent_kind(row.get("agent")) == "synthetic" or tier.startswith("cand_")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ok(row: dict[str, Any]) -> bool:
    return _is_true(row.get("success"))


def _empty(row: dict[str, Any]) -> bool:
    """A 200 that carried no output tokens: the caller got nothing for its money."""
    return _ok(row) and row.get("output_tokens") is not None and int(row["output_tokens"]) == 0


def _pcts(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    return round(_percentile(ordered, 0.5), 1), round(_percentile(ordered, 0.95), 1)


def _core(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ok = sum(_ok(r) for r in rows)
    lat = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
    p50, p95 = _pcts(lat)
    cost = sum(_num(r.get("estimated_cost")) for r in rows)
    out_tok = sum(int(r["output_tokens"]) for r in rows if r.get("output_tokens") is not None)
    in_tok = sum(int(r["input_tokens"]) for r in rows if r.get("input_tokens") is not None)
    fb = sum(1 for r in rows if _num(r.get("fallback_count")) > 0)
    trunc = sum(1 for r in rows if r.get("finish_reason") == "length")
    empty = sum(_empty(r) for r in rows)
    free = sum(1 for r in rows if r.get("estimated_cost") is not None and _num(r["estimated_cost"]) == 0 and _ok(r))
    return {
        "requests": n, "ok": ok, "failed": n - ok,
        "success_rate": round(ok * 100 / n, 2) if n else None,
        "cost": round(cost, 6), "cost_per_request": round(cost / n, 6) if n else None,
        "cost_per_m_output": round(cost * 1_000_000 / out_tok, 4) if out_tok else None,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "p50_ms": p50, "p95_ms": p95,
        "fallback_rate": round(fb * 100 / n, 2) if n else None,
        "truncation_rate": round(trunc * 100 / n, 2) if n else None,
        "empty_rate": round(empty * 100 / n, 2) if n else None,
        "free_requests": free,
    }


def _reliability(core: dict[str, Any], fallback_penalty: bool = True) -> float | None:
    """A transparent *reliability* proxy, not an answer-quality score: nothing here can see
    whether an answer was right. 100 x success x (1-truncated) x (1-empty) x (1-0.5 x fallback).
    The fallback term is dropped when scoring a *model*: a model that served after another
    one failed is the rescue, not the fault."""
    if not core["requests"]:
        return None
    s = (core["success_rate"] or 0) / 100
    t = (core["truncation_rate"] or 0) / 100
    e = (core["empty_rate"] or 0) / 100
    f = (core["fallback_rate"] or 0) / 100
    return round(100 * s * (1 - t) * (1 - e) * ((1 - 0.5 * f) if fallback_penalty else 1), 1)


def _group(rows: list[dict[str, Any]], key) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(key(r)), []).append(r)
    return out


def _top(rows: list[dict[str, Any]], key, limit: int = 3) -> list[dict[str, Any]]:
    total = len(rows) or 1
    counts: dict[str, int] = {}
    for r in rows:
        k = str(key(r))
        counts[k] = counts.get(k, 0) + 1
    return [{"name": k, "requests": v, "share": round(v * 100 / total, 1)}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def _series(rows: list[dict[str, Any]], start: datetime, end: datetime, width: timedelta) -> list[dict[str, Any]]:
    """Fixed, gap-free buckets so an idle minute draws as zero instead of vanishing."""
    step = width.total_seconds()
    origin = start.timestamp()
    count = max(1, int((end.timestamp() - origin) // step) + 1)
    buckets = [{"t": _iso(datetime.fromtimestamp(origin + i * step, timezone.utc)),
                "requests": 0, "failed": 0, "cost": 0.0, "lat": []} for i in range(count)]
    for r in rows:
        when = _parse_timestamp(r.get("timestamp"))
        if when is None:
            continue
        i = int((when.timestamp() - origin) // step)
        if 0 <= i < count:
            b = buckets[i]
            b["requests"] += 1
            b["failed"] += 0 if _ok(r) else 1
            b["cost"] += _num(r.get("estimated_cost"))
            if r.get("latency_ms") is not None:
                b["lat"].append(float(r["latency_ms"]))
    return [{"t": b["t"], "requests": b["requests"], "failed": b["failed"],
             "cost": round(b["cost"], 6), "p95_ms": _pcts(b["lat"])[1]} for b in buckets]


def build_overview(window: str, include_synthetic: bool) -> dict[str, Any]:
    span, width = WINDOWS[window]
    now = datetime.now(timezone.utc)
    start = (now - span) if span else None
    rows = _read(start)
    previous: list[dict[str, Any]] = []
    if span:
        previous = _read(start - span, start)
    synthetic_rows = sum(1 for r in rows if _is_synthetic(r))
    if not include_synthetic:
        rows = [r for r in rows if not _is_synthetic(r)]
        previous = [r for r in previous if not _is_synthetic(r)]

    if start is None:
        stamps = [t for r in rows if (t := _parse_timestamp(r.get("timestamp")))]
        start = min(stamps) if stamps else now - timedelta(days=1)
    kpis = _core(rows)
    kpis["reliability"] = _reliability(kpis)
    hours = max((now - start).total_seconds() / 3600, 1 / 60)
    kpis["spend_per_hour"] = round(kpis["cost"] / hours, 6)
    kpis["requests_per_minute"] = round(kpis["requests"] / (hours * 60), 2)
    unattributed = [r for r in rows if not (r.get("agent") or "").strip()]
    kpis["unattributed_cost"] = round(sum(_num(r.get("estimated_cost")) for r in unattributed), 6)
    kpis["unattributed_share"] = round(kpis["unattributed_cost"] * 100 / kpis["cost"], 1) if kpis["cost"] else 0.0
    kpis["unattributed_requests"] = len(unattributed)

    hints = []
    for hint, members in _group(unattributed, lambda r: r.get("client_hint") or "(not recorded)").items():
        stamps = [r["timestamp"] for r in members if r.get("timestamp")]
        hints.append({"hint": hint, "requests": len(members),
                      "cost": round(sum(_num(r.get("estimated_cost")) for r in members), 6),
                      "models": _top(members, lambda r: r.get("actual_model") or "(none)", 2),
                      "tiers": _top(members, lambda r: r.get("selected_tier") or "(none)", 2),
                      "last_seen": max(stamps) if stamps else None})
    hints.sort(key=lambda h: -h["cost"])

    prior = _core(previous) if span else None
    config = load_router_config()
    tiers_cfg = config.get("tiers") or {}

    callers = []
    for name, members in _group(rows, lambda r: (r.get("agent") or "").strip() or UNATTRIBUTED).items():
        core = _core(members)
        stamps = [r["timestamp"] for r in members if r.get("timestamp")]
        callers.append({
            "name": name, "kind": "unattributed" if name == UNATTRIBUTED else _agent_kind(name), **core,
            "share_requests": round(core["requests"] * 100 / len(rows), 1) if rows else 0,
            "share_cost": round(core["cost"] * 100 / kpis["cost"], 1) if kpis["cost"] else 0,
            "tiers": _top(members, lambda r: r.get("selected_tier") or "(none)"),
            "models": _top(members, lambda r: r.get("actual_model") or "(none)"),
            "tools_share": round(sum(_is_true(r.get("request_has_tools")) for r in members) * 100 / len(members), 1),
            "avg_input_tokens": round(core["input_tokens"] / max(1, sum(1 for r in members if r.get("input_tokens") is not None))),
            "last_seen": max(stamps) if stamps else None,
        })
    callers.sort(key=lambda c: -c["cost"])

    models = []
    for name, members in _group(rows, lambda r: r.get("actual_model") or "(none)").items():
        core = _core(members)
        declared_in = sorted(t for t, cfg in tiers_cfg.items()
                             if isinstance(cfg, dict) and name in [cfg.get("primary"), *(cfg.get("fallbacks") or [])])
        models.append({
            "name": name, **core, "reliability": _reliability(core, fallback_penalty=False),
            "share_cost": round(core["cost"] * 100 / kpis["cost"], 1) if kpis["cost"] else 0,
            "tiers": _top(members, lambda r: r.get("selected_tier") or "(none)"),
            "configured_in": declared_in, "is_free": name.endswith(":free"),
            "off_config": not declared_in,
        })
    models.sort(key=lambda m: -m["cost"])

    tiers = []
    for name, members in _group(rows, lambda r: r.get("selected_tier") or "(none)").items():
        core = _core(members)
        cfg = tiers_cfg.get(name) if isinstance(tiers_cfg.get(name), dict) else None
        tiers.append({"name": name, **core, "configured": cfg is not None,
                      "primary": (cfg or {}).get("primary"), "fallbacks": list((cfg or {}).get("fallbacks") or []),
                      "policy": (cfg or {}).get("provider_policy") or {}})
    for name, cfg in tiers_cfg.items():
        if isinstance(cfg, dict) and name not in {t["name"] for t in tiers}:
            tiers.append({"name": name, **_core([]), "configured": True, "primary": cfg.get("primary"),
                          "fallbacks": list(cfg.get("fallbacks") or []), "policy": cfg.get("provider_policy") or {}})
    tiers.sort(key=lambda t: -t["cost"])

    feed = []
    for r in reversed(rows[-FEED_ROWS:]):
        feed.append({
            "id": r.get("id"), "t": r.get("timestamp"),
            "caller": (r.get("agent") or "").strip() or UNATTRIBUTED,
            "tier": r.get("selected_tier"), "model": r.get("actual_model"),
            "latency_ms": r.get("latency_ms"), "cost": r.get("estimated_cost"),
            "in": r.get("input_tokens"), "out": r.get("output_tokens"),
            "status": r.get("http_status"), "ok": _ok(r), "fallbacks": int(_num(r.get("fallback_count"))),
            "finish": r.get("finish_reason"), "stream": _is_true(r.get("request_has_stream")),
            "tools": _is_true(r.get("request_has_tools")), "empty": _empty(r),
        })

    flow = build_topology(rows)
    flow_slim = {
        "nodes": [{"id": n["id"], "kind": n["kind"], "label": UNATTRIBUTED if n["label"] == "(untagged)" else n["label"], "requests": n["requests"],
                   "cost": round(n["cost"], 6), "failures": n["failures"],
                   "idle": bool(n["meta"].get("idle")), "off_config": bool(n["meta"].get("off_config")),
                   "off_chain": bool(n["meta"].get("off_chain"))}
                  for n in flow["nodes"] if n["kind"] in ("caller", "synthetic", "unknown", "tier", "model")],
        "edges": [{"from": e["from"], "to": e["to"], "requests": e["requests"], "cost": e["cost"],
                   "failures": e["failures"], "fallbacks": e["fallbacks"], "declared": e.get("declared"),
                   "p95_ms": e.get("p95_latency_ms")}
                  for e in flow["edges"] if not e["to"].startswith("upstream:")],
    }

    return {
        "generated_at": _iso(now), "window": window, "windows": list(WINDOWS),
        "start": _iso(start), "include_synthetic": include_synthetic, "synthetic_rows_hidden": 0 if include_synthetic else synthetic_rows,
        "config_available": config.get("available", False),
        "kpis": kpis, "prior": prior, "untagged_hints": hints,
        "series": _series(rows, start, now, width), "bucket_seconds": int(width.total_seconds()),
        "callers": callers, "models": models, "tiers": tiers, "flow": flow_slim, "feed": feed,
    }


router = APIRouter()


@router.get("/api/control/overview")
def control_overview(window: str = Query("1h"), include_synthetic: bool = Query(False)) -> Any:
    if window not in WINDOWS:
        return JSONResponse({"error": "unknown_window", "windows": list(WINDOWS)}, status_code=422)
    try:
        return build_overview(window, include_synthetic)
    except Exception as exc:  # a monitoring fault must never reach the routing path
        return JSONResponse({"error": "control_unavailable", "cause": exc.__class__.__name__}, status_code=503)


@router.get("/control", include_in_schema=False)
def control_page() -> Any:
    try:
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return HTMLResponse("<!doctype html><title>control center unavailable</title>"
                            "<p>static/control.html could not be read.</p>", status_code=503)


# ---------------------------------------------------------------------------------------------
# Risk matrix and agent profiles
# ---------------------------------------------------------------------------------------------

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DATA_TYPE_MAX, DATA_TYPES_MAX, NOTE_MAX = 40, 12, 200


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def _guard_write(request: Request) -> JSONResponse | None:
    """A page that can change routing must not be drivable from another website.

    Three checks: the request names a local host (blocks DNS rebinding), any Origin header must
    be this same host (blocks cross-site form posts and fetches), and a custom header that a
    cross-origin page cannot send without a CORS preflight (this app grants none) must be present.
    """
    host = _hostname(request.headers.get("host") or "")
    if host not in LOCAL_HOSTS:
        return JSONResponse({"error": "forbidden_host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc.lower() != (request.headers.get("host") or "").lower():
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    if request.headers.get("x-control-action") != "1":
        return JSONResponse({"error": "missing_control_header"}, status_code=403)
    return None


def _config_error(exc: config_store.ConfigError) -> JSONResponse:
    return JSONResponse({"error": "config_rejected", "message": str(exc)}, status_code=exc.status)


def build_risk(window: str) -> dict[str, Any]:
    span, _ = WINDOWS[window]
    now = datetime.now(timezone.utc)
    rows = [r for r in _read(now - span if span else None) if not _is_synthetic(r)]
    try:
        config, config_hash = config_store.read()
    except config_store.ConfigError:
        config, config_hash = {}, None  # the matrix still renders; writes will be refused without a hash
    profiles = config.get("agent_profiles") if isinstance(config.get("agent_profiles"), dict) else {}
    out = risk.build_risk(rows, profiles, config.get("policy_history"))
    out.update({"generated_at": _iso(now), "window": window, "config_hash": config_hash,
                "baselines": list(risk.BASELINES), "drafts": risk.DRAFT_PROFILES,
                "profiles": profiles, "audit": config_store.read_audit(8)})
    return out


@router.get("/api/control/risk")
def control_risk(window: str = Query("all")) -> Any:
    if window not in WINDOWS:
        return JSONResponse({"error": "unknown_window", "windows": list(WINDOWS)}, status_code=422)
    try:
        return build_risk(window)
    except config_store.ConfigError as exc:
        return _config_error(exc)
    except Exception as exc:
        return JSONResponse({"error": "control_unavailable", "cause": exc.__class__.__name__}, status_code=503)


def _clean_profile(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise config_store.ConfigError("agent name must be 1-60 characters")
    if not isinstance(value, dict) or value.get("baseline") not in risk.BASELINES:
        raise config_store.ConfigError(f"{name}: baseline must be one of {list(risk.BASELINES)}")
    data = value.get("data") or []
    if not isinstance(data, list) or len(data) > DATA_TYPES_MAX or not all(
            isinstance(d, str) and 0 < len(d.strip()) <= DATA_TYPE_MAX for d in data):
        raise config_store.ConfigError(f"{name}: data types must be a short list of short labels")
    note = value.get("note") or ""
    if not isinstance(note, str) or len(note) > NOTE_MAX:
        raise config_store.ConfigError(f"{name}: note is limited to {NOTE_MAX} characters")
    return {"baseline": value["baseline"], "data": [d.strip() for d in data], "note": note.strip()}


@router.put("/api/control/profiles")
async def control_put_profiles(request: Request) -> Any:
    refused = _guard_write(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    if not isinstance(body, dict) or not isinstance(body.get("profiles", {}), dict) \
            or not isinstance(body.get("remove", []), list):
        return JSONResponse({"error": "expected {base_hash, profiles, remove}"}, status_code=422)
    try:
        cleaned = {name: _clean_profile(name, value) for name, value in body.get("profiles", {}).items()}
        remove = [r for r in body.get("remove", []) if isinstance(r, str)]

        def mutate(cfg: dict[str, Any]) -> None:
            store = cfg.get("agent_profiles") if isinstance(cfg.get("agent_profiles"), dict) else {}
            store.update(cleaned)
            for name in remove:
                store.pop(name, None)
            cfg["agent_profiles"] = store

        names = sorted([*cleaned, *remove])
        result = config_store.update(mutate, base_hash=body.get("base_hash"),
                                     action="profiles:" + ",".join(names)[:200])
        return {"ok": True, **result}
    except config_store.ConfigError as exc:
        return _config_error(exc)


@router.get("/api/control/audit")
def control_audit(limit: int = Query(30, ge=1, le=200)) -> Any:
    return {"audit": config_store.read_audit(limit), "backups": config_store.list_backups(20)}


# ---------------------------------------------------------------- model & tier editor

TIER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9._~-]+/[A-Za-z0-9._:~-]+$")
MAX_CHAIN = 6
POLICIES = ("zdr", "no-collection", "none", "keep", "local")
LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,118}$")
PROTECTION_RANK = {"none": 0, "no-collection": 1, "zero-retention": 2, "local": 3}


class NeedsConfirmation(Exception):
    pass


def _ratio() -> float:
    """Input:output token ratio over the last 7 days, so prices can be blended the way you really spend."""
    try:
        rows = _read(datetime.now(timezone.utc) - timedelta(days=7))
    except Exception:
        return 72.0
    tin = sum(_num(r.get("input_tokens")) for r in rows)
    tout = sum(_num(r.get("output_tokens")) for r in rows)
    return round(min(max(tin / tout, 1.0), 200.0), 1) if tin and tout else 72.0


def _priced(model_id: str, prices: dict[str, dict[str, Any]], ratio: float) -> dict[str, Any]:
    m = prices.get(model_id)
    if not m:
        return {"id": model_id, "known": False}
    blended = None if m["in"] is None or m["out"] is None else round((ratio * m["in"] + m["out"]) / (ratio + 1), 4)
    return {"id": model_id, "known": True, "name": m["name"], "in": m["in"], "out": m["out"],
            "blended": blended, "free": m["free"], "context": m["context"], "tools": m["tools"]}


def _priced_local(model_id: str, installed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    m = installed.get(model_id)
    if not m:
        return {"id": model_id, "known": False, "local": True}
    return {"id": model_id, "known": True, "local": True, "name": model_id, "in": 0, "out": 0, "blended": 0, "free": True,
            "context": None, "tools": None, "size_gb": m["size_gb"], "params": m["params"], "embedding": m["embedding"]}


@router.get("/api/control/local-models")
def control_local_models(refresh: bool = Query(False)) -> Any:
    try:
        config, _ = config_store.read()
    except config_store.ConfigError as exc:
        return _config_error(exc)
    models, status = local_models.load(config, force=refresh)
    if models is None:
        return JSONResponse({"error": "ollama_unavailable", "status": status, "models": []}, status_code=503)
    return {"status": status, "models": sorted(models, key=lambda m: (m["embedding"], m["id"]))}


@router.get("/api/control/catalog")
def control_catalog(q: str = Query("", max_length=80), free: bool = Query(False), tools: bool = Query(False),
                    limit: int = Query(40, ge=1, le=100), refresh: bool = Query(False)) -> Any:
    models, status = catalog.load(force=refresh)
    if models is None:
        return JSONResponse({"error": "catalog_unavailable", "status": status, "models": []}, status_code=503)
    return {"status": status, "ratio": _ratio(), "models": catalog.search(models, q, free, tools, limit)}


@router.get("/api/control/tiers")
def control_tiers() -> Any:
    try:
        config, config_hash = config_store.read()
    except config_store.ConfigError as exc:
        return _config_error(exc)
    models, status = catalog.load()
    prices, ratio = catalog.lookup(models), _ratio()
    wants_local = any(x.get("provider") == "ollama" for x in (config.get("tiers") or {}).values())
    installed_list, local_status = local_models.load(config) if wants_local else (None, None)
    installed = {m["id"]: m for m in installed_list or []}
    tiers = {}
    for name, tier in (config.get("tiers") or {}).items():
        policy = tier.get("provider_policy")
        chain = [tier.get("primary"), *(tier.get("fallbacks") or [])]
        local = tier.get("provider") == "ollama"
        tiers[name] = {"primary": tier.get("primary"), "fallbacks": list(tier.get("fallbacks") or []),
                       "provider": tier.get("provider"), "policy": policy,
                       "protection": "local" if local else risk.protection_of(policy),
                       "min_max_tokens": tier.get("min_max_tokens"),
                       "chain": [(_priced_local(m, installed) if local else _priced(m, prices, ratio))
                                 for m in chain if isinstance(m, str)]}
    return {"config_hash": config_hash, "ratio": ratio, "catalog": status, "local": local_status, "tiers": tiers,
            "policies": list(POLICIES), "audit": config_store.read_audit(8)}


def _apply_policy(existing: Any, choice: str) -> dict[str, Any] | None:
    if choice == "local":
        return {"local": True}
    if choice == "keep":
        return existing if isinstance(existing, dict) else None
    base = {k: v for k, v in (existing or {}).items() if k not in ("zdr", "data_collection")}
    if choice == "zdr":
        base["zdr"] = True
    elif choice == "no-collection":
        base["data_collection"] = "deny"
    return base or None


def _snapshot(cfg: dict[str, Any]) -> dict[str, str]:
    return {t: risk.protection_of(x.get("provider_policy")) for t, x in (cfg.get("tiers") or {}).items()}


def _check_local_chain(ids: list[Any], installed: dict[str, dict[str, Any]], verified: bool, keep: frozenset[str]) -> None:
    if not ids or len(ids) > MAX_CHAIN:
        raise config_store.ConfigError(f"a tier needs 1 to {MAX_CHAIN} models (primary plus fallbacks)")
    for model_id in ids:
        if not isinstance(model_id, str) or not LOCAL_ID.match(model_id):
            raise config_store.ConfigError(f"{model_id!r} is not a valid Ollama model name")
        if verified and model_id not in installed and model_id not in keep:
            raise config_store.ConfigError(f"{model_id} is not installed in Ollama (run: ollama pull {model_id})")
        if installed.get(model_id, {}).get("embedding"):
            raise config_store.ConfigError(f"{model_id} is an embedding model, not a chat model")


def _check_chain(tier: str, ids: list[Any], prices: dict[str, dict[str, Any]], verified: bool,
                 keep: frozenset[str] = frozenset()) -> None:
    if not ids or len(ids) > MAX_CHAIN:
        raise config_store.ConfigError(f"a tier needs 1 to {MAX_CHAIN} models (primary plus fallbacks)")
    for model_id in ids:
        if not isinstance(model_id, str) or not MODEL_ID.match(model_id):
            raise config_store.ConfigError(f"{model_id!r} is not an OpenRouter model id (expected vendor/model)")
        if verified and model_id not in prices and model_id not in keep:
            raise config_store.ConfigError(f"{model_id} is not in the OpenRouter catalog")
        if tier == "free" and not (model_id.endswith(":free") or prices.get(model_id, {}).get("free")):
            raise config_store.ConfigError(f"{model_id} is not free; the free tier only takes free models")


@router.put("/api/control/tiers")
async def control_put_tiers(request: Request) -> Any:
    refused = _guard_write(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    if not isinstance(body, dict) or body.get("action") not in ("set", "create", "delete") \
            or not isinstance(body.get("tier"), str):
        return JSONResponse({"error": "expected {base_hash, action: set|create|delete, tier, ...}"}, status_code=422)
    action, tier, confirm = body["action"], body["tier"], body.get("confirm") is True
    new_provider = body.get("provider", "openrouter")
    if action == "create" and new_provider not in ("openrouter", "ollama"):
        return JSONResponse({"error": "provider must be 'openrouter' or 'ollama'"}, status_code=422)
    choice = body.get("policy", "keep" if action == "set" else ("local" if new_provider == "ollama" else "zdr"))
    if choice not in POLICIES:
        return JSONResponse({"error": f"policy must be one of {list(POLICIES)}"}, status_code=422)
    try:
        current_cfg, _ = config_store.read()
    except config_store.ConfigError as exc:
        return _config_error(exc)
    tier_provider = new_provider if action == "create" else (current_cfg.get("tiers", {}).get(tier) or {}).get("provider")
    if tier_provider == "ollama":
        installed_list, _ = local_models.load(current_cfg)
        installed, verified, prices = {m["id"]: m for m in installed_list or []}, installed_list is not None, {}
    else:
        models, _ = catalog.load()
        prices, verified, installed = catalog.lookup(models), models is not None, {}

    def mutate(cfg: dict[str, Any]) -> None:
        tiers = cfg.setdefault("tiers", {})
        before = _snapshot(cfg)
        if action == "delete":
            if tier not in tiers:
                raise config_store.ConfigError(f"no tier named {tier!r}", 404)
            if len(tiers) == 1:
                raise config_store.ConfigError("cannot delete the last tier")
            referenced = [k for k, v in cfg.items() if k != "tiers" and v == tier]
            if referenced:
                raise config_store.ConfigError(f"tier {tier!r} is still referenced by config key {referenced[0]!r}")
            if not confirm:
                raise NeedsConfirmation(f"Delete tier {tier!r}? Clients that still request it will get an error.")
            del tiers[tier]
        else:
            if action == "create":
                if not TIER_NAME.match(tier):
                    raise config_store.ConfigError("tier name: lowercase letters, digits, - or _ (max 31)")
                if tier in tiers:
                    raise config_store.ConfigError(f"tier {tier!r} already exists", 409)
                tiers[tier] = {"provider": new_provider}
            elif tier not in tiers:
                raise config_store.ConfigError(f"no tier named {tier!r}", 404)
            fallbacks = body.get("fallbacks") or []
            if not isinstance(fallbacks, list):
                raise config_store.ConfigError("fallbacks must be a list")
            chain = [body.get("primary"), *fallbacks]
            current = tiers[tier]
            keep = frozenset(m for m in [current.get("primary"), *(current.get("fallbacks") or [])] if isinstance(m, str))
            is_local = tiers[tier].get("provider") == "ollama"
            if is_local:
                _check_local_chain(chain, installed, verified, keep)
                choice_used = "local"  # a local tier is always private; the privacy setting does not apply
            else:
                if choice == "local":
                    raise config_store.ConfigError("only a local (Ollama) tier can use the 'local' privacy level")
                _check_chain(tier, chain, prices, verified, keep)
                choice_used = choice
            tiers[tier]["primary"], tiers[tier]["fallbacks"] = chain[0], chain[1:]
            policy = _apply_policy(tiers[tier].get("provider_policy"), choice_used)
            if policy:
                tiers[tier]["provider_policy"] = policy
            else:
                tiers[tier].pop("provider_policy", None)
        after = _snapshot(cfg)
        weaker = [t for t in before if t in after and PROTECTION_RANK[after[t]] < PROTECTION_RANK[before[t]]]
        if weaker and not confirm:
            raise NeedsConfirmation("This lowers privacy protection on: " + ", ".join(weaker)
                                    + ". Traffic on those tiers may be retained or used for training by providers.")
        if after != before:  # keep the risk matrix's policy eras truthful
            history = copy.deepcopy(risk.normalise_history(cfg.get("policy_history")))
            history.append({"since": _iso(datetime.now(timezone.utc)), "note": f"Control Center: {action} {tier}",
                            "tiers": {t: copy.deepcopy(x.get("provider_policy")) for t, x in cfg["tiers"].items()}})
            cfg["policy_history"] = history

    try:
        result = config_store.update(mutate, base_hash=body.get("base_hash"), action=f"tier:{action}:{tier}"[:120])
    except NeedsConfirmation as exc:
        return JSONResponse({"error": "needs_confirmation", "message": str(exc)}, status_code=409)
    except config_store.ConfigError as exc:
        return _config_error(exc)
    return {"ok": True, "catalog_verified": verified, **result}


@router.post("/api/control/restore")
async def control_restore(request: Request) -> Any:
    refused = _guard_write(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return JSONResponse({"error": "expected {base_hash, name}"}, status_code=422)
    try:
        return {"ok": True, **config_store.restore(body["name"], base_hash=body.get("base_hash"))}
    except config_store.ConfigError as exc:
        return _config_error(exc)


# ---------------------------------------------------------------- egress monitor

@router.get("/api/control/egress")
def control_egress(window: str = Query("24h")) -> Any:
    if window not in WINDOWS:
        return JSONResponse({"error": "unknown_window", "windows": list(WINDOWS)}, status_code=422)
    now = datetime.now(timezone.utc)
    active = egress.read_active()
    out = egress.build(egress.read_log(), active, now, WINDOWS[window][0])
    out.update({"window": window, "generated_at": _iso(now), "collector": egress.collector_status(active, now),
                "caveat": "Shows which program held a connection to which AI provider. An open connection is not proof "
                          "that data was sent, very short connections can be missed, and connections already open when the collector started "
                          "are timed from that moment. Connections on addresses shared with other websites (common behind Cloudflare) "
                          "may carry the wrong provider label. No content is ever read."})
    return out


def _egress_audit(action: str) -> None:
    config_store._audit({"at": config_store._now(), "actor": "control-center", "action": action[:160], "paths": []})


@router.post("/api/control/egress/kill")
async def control_egress_kill(request: Request) -> Any:
    refused = _guard_write(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected {pid, proc}"}, status_code=422)
    try:
        target = egress.check_kill(egress.read_active(), body.get("pid"), body.get("proc"))
        message = egress.run_kill(target["pid"])
    except egress.ActionError as exc:
        return JSONResponse({"error": "refused", "message": str(exc)}, status_code=exc.status)
    except Exception as exc:
        return JSONResponse({"error": "kill_failed", "message": exc.__class__.__name__}, status_code=502)
    _egress_audit(f"egress:kill:{target['proc']}:{target['pid']}")
    return {"ok": True, "message": message, **target}


@router.post("/api/control/egress/script")
async def control_egress_script(request: Request) -> Any:
    """Returns a generated, self-elevating firewall script for the browser to download. Nothing is executed here."""
    refused = _guard_write(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=422)
    if not isinstance(body, dict) or body.get("kind") not in ("block", "unblock", "unblock_all"):
        return JSONResponse({"error": "expected {kind: block|unblock|unblock_all, ...}"}, status_code=422)
    try:
        if body["kind"] == "block":
            records = egress._everything(egress.read_active(), egress.read_log(), datetime.now(timezone.utc))
            filename, text, note = egress.block_script(str(body.get("proc") or ""), str(body.get("provider") or ""),
                                                       str(body.get("scope") or ""), records)
        elif body["kind"] == "unblock":
            filename, text, note = egress.unblock_script(body.get("rule"))
        else:
            filename, text, note = egress.unblock_script(None)
    except egress.ActionError as exc:
        return JSONResponse({"error": "refused", "message": str(exc)}, status_code=exc.status)
    _egress_audit(f"egress:script:{body['kind']}:{note.get('rule')}")
    return {"ok": True, "filename": filename, "script": text, "summary": note}


@router.get("/api/control/inbound")
def control_inbound(window: str = Query("24h")) -> Any:
    if window not in WINDOWS:
        return JSONResponse({"error": "unknown_window", "windows": list(WINDOWS)}, status_code=422)
    now = datetime.now(timezone.utc)
    active = egress.read_active()
    out = egress.build_inbound(egress.read_ingress(), active, now, WINDOWS[window][0])
    out.update({"window": window, "generated_at": _iso(now), "collector": egress.collector_status(active, now),
                "caveat": "A listening port is not the same as reachable: Windows Firewall still decides whether other devices can "
                          "connect. Only connection metadata (program, port, remote address, duration) is recorded, never content."})
    return out


# ---- canary quality suite ---------------------------------------------------------------------

@router.get("/api/control/canary")
def control_canary() -> Any:
    cfg = load_router_config()
    tiers = cfg.get("tiers") or {}
    summary = canary.summarize(canary.read_results())
    return {"summary": summary, "status": dict(canary.RUNNER.state),
            "tiers": {name: {"provider": (spec or {}).get("provider", "openrouter"),
                             "primary": (spec or {}).get("primary")} for name, spec in tiers.items()},
            "task_count": len(canary.TASKS)}


@router.post("/api/control/canary/run")
async def control_canary_run(request: Request) -> Any:
    blocked = _guard_write(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    cfg_tiers = load_router_config().get("tiers") or {}
    chosen = body.get("tiers")
    if not isinstance(chosen, list) or not chosen or len(chosen) > 12 or \
            any(not isinstance(n, str) or n not in cfg_tiers for n in chosen):
        return JSONResponse({"error": "unknown_tier", "message": "choose one or more configured tiers"}, status_code=400)
    chosen = list(dict.fromkeys(chosen))
    ids = body.get("tasks")
    if ids is not None and (not isinstance(ids, list) or any(i not in canary.TASK_IDS for i in ids)):
        return JSONResponse({"error": "unknown_task"}, status_code=400)
    cloud = [n for n in chosen if (cfg_tiers[n] or {}).get("provider", "openrouter") != "ollama"]
    if cloud and body.get("confirm") is not True:
        return JSONResponse({"error": "confirm_required",
                             "message": "This sends the test prompts to cloud models and costs real money: " + ", ".join(cloud)},
                            status_code=409)
    host = request.headers.get("host") or "127.0.0.1:6060"
    if not canary.RUNNER.start(chosen, ids, url=f"http://{host}/v1/chat/completions"):
        return JSONResponse({"error": "already_running"}, status_code=409)
    _egress_audit("canary-run")
    return {"started": True, "status": dict(canary.RUNNER.state)}
