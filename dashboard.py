"""Read-only monitoring dashboard for openclaw-router.

Serves the browser UI at GET /dashboard and two JSON APIs:
  GET /api/dashboard/summary?recent=100
  GET /api/dashboard/recent?limit=100

Design constraints enforced here:
  * Every telemetry read goes through telemetry_report.connect_read_only, which opens
    SQLite with mode=ro, so a dashboard query cannot write.
  * Only the columns named in SUMMARY_COLUMNS / RECENT_COLUMNS are ever selected.
    Prompts and responses are not stored in telemetry, and this whitelist keeps the
    dashboard from reading any column a future schema might add for content.
  * Aggregation is delegated to telemetry_report.summarize_routing_rows so the numbers
    on the page match `python telemetry_report.py --routing`.
  * Each handler is individually guarded: a data-source fault returns 503 instead of
    propagating, and nothing here is imported by the request path that forwards models.
"""

from __future__ import annotations

import sqlite3
import statistics
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import telemetry_report
from telemetry_report import _is_true, _percentile, connect_read_only, summarize_routing_rows

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
PAGE_PATH = STATIC_DIR / "dashboard.html"
CHART_JS_PATH = STATIC_DIR / "vendor" / "chart.umd.min.js"
SNAPSHOTS_PATH = ROOT / "dashboard_snapshots.json"
# Read directly rather than via app.load_config(): app imports this module, so importing
# app back would be circular. The file itself is owned by app.py.
CONFIG_PATH = ROOT / "router_config.json"

# Columns read for aggregate reporting. Everything is operational telemetry: no body,
# no message, no token text.
SUMMARY_COLUMNS = (
    "id", "timestamp", "requested_tier", "selected_tier", "routing_automatic", "routing_reason",
    "actual_model", "attempted_models", "input_tokens", "output_tokens", "latency_ms",
    "estimated_cost", "http_status", "success", "fallback_count", "budget_exhausted",
    "finish_reason", "agent", "attempt_outcomes",
)

# Columns the recent-request table may show. A strict subset of SUMMARY_COLUMNS and
# nothing else, so the UI cannot render a field the API does not send.
RECENT_COLUMNS = (
    "timestamp", "requested_tier", "selected_tier", "routing_automatic", "routing_reason",
    "actual_model", "agent", "latency_ms", "estimated_cost", "http_status", "success",
    "fallback_count", "budget_exhausted",
)

# Grouping adds request *shape* to the same operational-only rule: still no body, no
# message, no token text. Tool usage is a request-structure flag, not content.
GROUPING_COLUMNS = SUMMARY_COLUMNS + ("request_has_tools", "task_type")

WINDOWS = (25, 50, 100, 500)
MIN_WINDOW, MAX_WINDOW = 1, 1000
SCOPE_NAMES = ("recent", "today", "last_24_hours", "last_7_days", "custom", "since_snapshot")


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _parse_timestamp(value: Any, *, assume_local: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone() if assume_local else parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_snapshots() -> list[dict[str, Any]]:
    """Read dashboard-only snapshot metadata; never open the telemetry DB writable."""
    try:
        value = json.loads(SNAPSHOTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError("snapshot metadata must be a JSON list")
    snapshots = []
    for item in value:
        if (isinstance(item, dict) and isinstance(item.get("name"), str)
                and isinstance(item.get("after_telemetry_id"), int)):
            snapshots.append({
                "name": item["name"], "created_at": item.get("created_at"),
                "after_telemetry_id": item["after_telemetry_id"], "note": item.get("note"),
                "summary": item.get("summary", ""), "goal": item.get("goal", ""),
                "context": item.get("context", []),
                "benchmark_findings": item.get("benchmark_findings", []),
                "notes": item.get("notes", item.get("note", "")),
            })
    return snapshots


def db_path() -> Path:
    """Resolved through telemetry_report so one setting governs both readers."""
    return telemetry_report.DB_PATH


def _has_telemetry_table(db: sqlite3.Connection) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'").fetchone())


def read_window(limit: int, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return the most recent `limit` rows, oldest first. Empty if the database or table is absent."""
    path = db_path()
    if not path.exists():
        return []
    with connect_read_only(path) as db:
        if not _has_telemetry_table(db):
            return []
        sql = f"SELECT {', '.join(columns)} FROM telemetry ORDER BY id DESC LIMIT ?"
        rows = db.execute(sql, (int(limit),)).fetchall()
    return [dict(row) for row in reversed(rows)]


def read_scoped_rows(scope: str, recent: int, start: str | None = None,
                     end: str | None = None, snapshot_name: str | None = None,
                     columns: tuple[str, ...] = SUMMARY_COLUMNS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one authoritative row set for both summary and recent-table APIs."""
    if scope not in SCOPE_NAMES:
        raise HTTPException(status_code=422, detail="unsupported dashboard scope")
    path = db_path()
    rows: list[dict[str, Any]] = []
    if scope == "recent":
        rows = read_window(recent, columns)
        return rows, {"scope": scope, "requested": recent, "returned": len(rows),
                      "available_windows": list(WINDOWS)}
    if path.exists():
        with connect_read_only(path) as db:
            if _has_telemetry_table(db):
                rows = [dict(row) for row in db.execute(
                    f"SELECT {', '.join(columns)} FROM telemetry ORDER BY id"
                ).fetchall()]

    metadata: dict[str, Any] = {"scope": scope, "returned": 0}
    if scope in ("today", "last_24_hours", "last_7_days"):
        local_now = _now_local()
        now_utc = local_now.astimezone(timezone.utc)
        if scope == "today":
            local_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
            start_utc = local_start.astimezone(timezone.utc)
        else:
            start_utc = now_utc - timedelta(hours=24 if scope == "last_24_hours" else 24 * 7)
        rows = [row for row in rows if (when := _parse_timestamp(row.get("timestamp"))) is not None
                and start_utc <= when <= now_utc]
        metadata.update({"start": start_utc.isoformat(), "end": now_utc.isoformat()})
    elif scope == "custom":
        if not start or not end:
            raise HTTPException(status_code=422, detail="custom scope requires start and end")
        start_dt, end_dt = _parse_timestamp(start, assume_local=True), _parse_timestamp(end, assume_local=True)
        if start_dt is None or end_dt is None or end_dt < start_dt:
            raise HTTPException(status_code=422, detail="custom date range is invalid")
        rows = [row for row in rows if (when := _parse_timestamp(row.get("timestamp"))) is not None
                and start_dt <= when <= end_dt]
        metadata.update({"start": start_dt.isoformat(), "end": end_dt.isoformat()})
    elif scope == "since_snapshot":
        snapshots = load_snapshots()
        snapshot = next((item for item in snapshots if item["name"] == snapshot_name), None)
        if snapshot is None:
            raise HTTPException(status_code=422, detail="unknown snapshot")
        rows = [row for row in rows if int(row.get("id") or 0) > snapshot["after_telemetry_id"]]
        metadata.update({"snapshot": snapshot, "requests_since_snapshot": len(rows)})

    metadata["returned"] = len(rows)
    return rows, metadata


def build_health() -> dict[str, Any]:
    status: dict[str, Any] = {
        "database_available": False,
        "database_exists": db_path().exists(),
        "total_telemetry_rows": None,
        "oldest_timestamp": None,
        "newest_timestamp": None,
    }
    try:
        if status["database_exists"]:
            with connect_read_only(db_path()) as db:
                db.execute("SELECT 1").fetchone()
                status["database_available"] = True
                if _has_telemetry_table(db):
                    status["total_telemetry_rows"] = db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
                    oldest, newest = db.execute("SELECT MIN(timestamp), MAX(timestamp) FROM telemetry").fetchone()
                    status["oldest_timestamp"], status["newest_timestamp"] = oldest, newest
    except sqlite3.Error:
        status["database_available"] = False
    return status


def build_cards(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Window-level summary cards, using the same rounding conventions as telemetry_report."""
    total = len(rows)
    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    costs = [float(row["estimated_cost"]) for row in rows if row.get("estimated_cost") is not None]
    successes = sum(_is_true(row.get("success")) for row in rows)
    fallback_requests = sum(float(row.get("fallback_count") or 0) > 0 for row in rows)
    exhausted_requests = sum(_is_true(row.get("budget_exhausted")) for row in rows)
    return {
        "total_requests": total,
        "successful_requests": successes,
        "failed_requests": total - successes,
        "success_rate_percent": round(successes * 100 / total, 2) if total else 0.0,
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "median_latency_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 2) if latencies else None,
        "latency_samples": len(latencies),
        "total_estimated_cost": round(sum(costs), 8),
        "average_cost_per_request": round(sum(costs) / total, 8) if total else None,
        "requests_with_cost": len(costs),
        "fallback_requests": fallback_requests,
        "fallback_rate_percent": round(fallback_requests * 100 / total, 2) if total else 0.0,
        "budget_exhausted_requests": exhausted_requests,
        "budget_exhaustion_rate_percent": round(exhausted_requests * 100 / total, 2) if total else 0.0,
    }


def build_summary(recent: int, scope: str = "recent", start: str | None = None,
                  end: str | None = None, snapshot_name: str | None = None) -> dict[str, Any]:
    rows, window = read_scoped_rows(scope, recent, start, end, snapshot_name)
    report = summarize_routing_rows(rows)
    total_cost = sum(float(row.get("estimated_cost") or 0) for row in rows)
    tier_costs: dict[str, dict[str, Any]] = {}
    model_costs: dict[str, float] = {}
    for row in rows:
        tier = row.get("selected_tier") or "unknown"
        model = row.get("actual_model") or "unknown"
        cost = float(row.get("estimated_cost") or 0)
        tier_bucket = tier_costs.setdefault(tier, {"requests": 0, "total_cost": 0.0})
        tier_bucket["requests"] += 1
        tier_bucket["total_cost"] += cost
        model_costs[model] = model_costs.get(model, 0.0) + cost
    for values in tier_costs.values():
        values["total_cost"] = round(values["total_cost"], 8)
        values["average_cost_per_request"] = round(values["total_cost"] / values["requests"], 8) if values["requests"] else None
        values["percent_of_total"] = round(values["total_cost"] * 100 / total_cost, 2) if total_cost else 0.0
    model_costs = {model: round(cost, 8) for model, cost in model_costs.items()}
    cumulative = 0.0
    cost_over_time = []
    for row in rows:
        cost = float(row.get("estimated_cost") or 0)
        cumulative += cost
        cost_over_time.append({"timestamp": row.get("timestamp"), "cost": round(cost, 8),
                               "cumulative_cost": round(cumulative, 8)})
    if scope == "recent" and start is None and end is None and snapshot_name is None:
        # Preserve the original API shape for callers using ?recent=N.
        window = {"requested": recent, "returned": len(rows), "available_windows": list(WINDOWS)}
    return {
        "window": window,
        "cards": build_cards(rows),
        "cost_analytics": {
            "total_cost": round(total_cost, 8),
            "average_cost_per_request": round(total_cost / len(rows), 8) if rows else None,
            "total_cost_by_tier": tier_costs,
            "total_cost_by_model": model_costs,
            "cost_over_time": cost_over_time,
        },
        "health": build_health(),
        "routing": report,
        "models": {
            "actual_model_distribution": report["actual_model_distribution"],
            "attempted_model_sequences": report["attempted_model_sequences"],
            "tier_operations": report["tier_operations"],
        },
    }


def build_recent(limit: int, scope: str = "recent", start: str | None = None,
                 end: str | None = None, snapshot_name: str | None = None) -> dict[str, Any]:
    rows, window = read_scoped_rows(scope, limit, start, end, snapshot_name)
    visible = [{key: row.get(key) for key in RECENT_COLUMNS} for row in reversed(rows)]
    if scope == "recent" and start is None and end is None and snapshot_name is None:
        window = {"requested": limit, "returned": len(rows)}
    return {"window": window, "requests": visible}


def _bucket_key(when: datetime, bucket: str) -> str:
    local = when.astimezone()
    return local.strftime("%Y-%m-%d %H:00") if bucket == "hour" else local.strftime("%Y-%m-%d")


def _daily_totals(timed: list[tuple[datetime, dict[str, Any]]]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    for when, row in timed:
        day = days.setdefault(when.astimezone().strftime("%Y-%m-%d"), {
            "day": when.astimezone().strftime("%Y-%m-%d"), "requests": 0,
            "cost": 0.0, "input_tokens": 0, "output_tokens": 0})
        day["requests"] += 1
        day["cost"] += float(row.get("estimated_cost") or 0)
        day["input_tokens"] += int(row.get("input_tokens") or 0)
        day["output_tokens"] += int(row.get("output_tokens") or 0)
    ordered = [days[key] for key in sorted(days)]
    for day in ordered:
        day["cost"] = round(day["cost"], 8)
    return ordered


def _project(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Least-squares daily trend plus a flat recent-mean baseline, for cost and input tokens.

    Deliberately simple and labelled: it extrapolates the observed day buckets and nothing
    else. With fewer than two day buckets no trend is meaningful, so say so instead of
    inventing one.
    """
    n = len(days)
    if n < 2:
        return {"available": False, "days_observed": n,
                "reason": "at least two day buckets are needed to fit a trend"}
    xs = list(range(n))
    mean_x = sum(xs) / n

    def fit(values: list[float]) -> tuple[float, float]:
        mean_y = sum(values) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom if denom else 0.0
        return slope, mean_y - slope * mean_x

    def horizon(slope: float, intercept: float, mean: float, count: int) -> dict[str, float]:
        linear = sum(max(0.0, intercept + slope * (n - 1 + i)) for i in range(1, count + 1))
        return {"linear_trend": round(linear, 6), "recent_mean": round(mean * count, 6)}

    result: dict[str, Any] = {
        "available": True, "days_observed": n, "days": [day["day"] for day in days],
        "method": ("least-squares fit over observed day buckets; recent_mean is the "
                   "flat-rate alternative; partial days are included as observed"),
    }
    for field in ("cost", "input_tokens"):
        values = [float(day[field]) for day in days]
        slope, intercept = fit(values)
        mean = sum(values) / n
        linear30 = sum(max(0.0, intercept + slope * (n - 1 + i)) for i in range(1, 31))
        flat30 = mean * 30
        divergence = (linear30 / flat30) if flat30 > 0 else None
        result[field] = {
            "per_day_recent_mean": round(mean, 6),
            "per_day_trend_slope": round(slope, 6),
            "next_7_days": horizon(slope, intercept, mean, 7),
            "next_30_days": {"linear_trend": round(linear30, 6), "recent_mean": round(flat30, 6)},
            "divergence_ratio_30d": round(divergence, 2) if divergence is not None else None,
        }
    # A least-squares line through a handful of days can explode. Say so loudly rather
    # than letting one big day masquerade as a monthly forecast.
    warnings: list[str] = []
    if n < 7:
        warnings.append(f"only {n} day buckets observed - treat every projection as indicative")
    cost_div = result["cost"].get("divergence_ratio_30d")
    if cost_div is not None and (cost_div > 3 or cost_div < 1 / 3):
        warnings.append(
            f"linear 30-day cost is {cost_div:.1f}x the flat-rate estimate - the trend fit is "
            "unstable at this sample size; prefer recent_mean")
    result["confidence"] = "low" if n < 7 else ("moderate" if n < 14 else "ok")
    result["warnings"] = warnings
    return result


def build_timeseries(scope: str, recent: int, start: str | None = None,
                     end: str | None = None, snapshot_name: str | None = None) -> dict[str, Any]:
    rows, window = read_scoped_rows(scope, recent, start, end, snapshot_name)
    timed = [(when, row) for row in rows
             if (when := _parse_timestamp(row.get("timestamp"))) is not None]
    timed.sort(key=lambda item: item[0])
    span_hours = (timed[-1][0] - timed[0][0]).total_seconds() / 3600 if timed else 0.0
    bucket = "hour" if span_hours <= 72 else "day"

    buckets: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    for when, row in timed:
        b = buckets.setdefault(_bucket_key(when, bucket), {
            "requests": 0, "ok": 0, "failed": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0, "latencies": []})
        b["requests"] += 1
        b["ok" if _is_true(row.get("success")) else "failed"] += 1
        b["cost"] += float(row.get("estimated_cost") or 0)
        b["input_tokens"] += int(row.get("input_tokens") or 0)
        b["output_tokens"] += int(row.get("output_tokens") or 0)
        if row.get("latency_ms") is not None:
            b["latencies"].append(float(row["latency_ms"]))
        a = agents.setdefault(row.get("agent") or "(unattributed)", {
            "requests": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0})
        a["requests"] += 1
        a["cost"] += float(row.get("estimated_cost") or 0)
        a["input_tokens"] += int(row.get("input_tokens") or 0)
        a["output_tokens"] += int(row.get("output_tokens") or 0)

    series = []
    for key in sorted(buckets):
        b = buckets[key]
        series.append({
            "bucket": key, "requests": b["requests"], "ok": b["ok"], "failed": b["failed"],
            "cost": round(b["cost"], 8), "input_tokens": b["input_tokens"],
            "output_tokens": b["output_tokens"],
            "p95_latency_ms": round(_percentile(b["latencies"], 0.95), 2) if b["latencies"] else None,
        })
    for a in agents.values():
        a["cost"] = round(a["cost"], 8)
    return {
        "window": window, "bucket": bucket, "series": series,
        "agents": dict(sorted(agents.items(), key=lambda kv: -kv[1]["cost"])),
        "days": _daily_totals(timed), "projection": _project(_daily_totals(timed)),
    }


router = APIRouter()


INPUT_BANDS = ("<2k", "2k-10k", "10k-50k", "50k+", "(no usage captured)")


def _input_band(value: Any) -> str:
    if value is None:
        return "(no usage captured)"
    tokens = float(value)
    if tokens < 2000:
        return "<2k"
    if tokens < 10000:
        return "2k-10k"
    if tokens < 50000:
        return "10k-50k"
    return "50k+"


def _value_counts(rows: list[dict[str, Any]], pick: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """Counts as a list of labelled entries, never as a dict keyed by the value.

    Group labels come from data (an agent name, a model id, a finish reason), and the
    dashboard's content guard forbids data-derived keys: a tier or agent named `text`
    would otherwise look like a leaked field.
    """
    counts: dict[str, int] = {}
    for row in rows:
        key = str(pick(row))
        counts[key] = counts.get(key, 0) + 1
    return [{"name": name, "requests": n}
            for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _aggregate(rows: list[dict[str, Any]], window_requests: int, window_cost: float) -> dict[str, Any]:
    """One group's numbers.

    Every average names the sample it was taken over: token and cost columns are NULL
    whenever upstream usage never arrived, so a group mean computed over 900 of 1,000
    rows must not read as though it covered all of them.
    """
    total = len(rows)
    lat = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
    ins = [float(r["input_tokens"]) for r in rows if r.get("input_tokens") is not None]
    outs = [float(r["output_tokens"]) for r in rows if r.get("output_tokens") is not None]
    costs = [float(r["estimated_cost"]) for r in rows if r.get("estimated_cost") is not None]
    ok = sum(_is_true(r.get("success")) for r in rows)
    return {
        "requests": total,
        "successful": ok,
        "failed": total - ok,
        "success_rate_percent": round(ok * 100 / total, 2) if total else 0.0,
        "share_of_requests_percent": round(total * 100 / window_requests, 2) if window_requests else 0.0,
        "share_of_cost_percent": round(sum(costs) * 100 / window_cost, 2) if window_cost else 0.0,
        "input_tokens_total": int(sum(ins)),
        "input_tokens_avg": round(sum(ins) / len(ins)) if ins else None,
        "output_tokens_total": int(sum(outs)),
        "output_tokens_avg": round(sum(outs) / len(outs)) if outs else None,
        "cost_total": round(sum(costs), 8),
        "cost_avg": round(sum(costs) / len(costs), 8) if costs else None,
        "latency_avg_ms": round(sum(lat) / len(lat), 1) if lat else None,
        "latency_p95_ms": round(_percentile(lat, 0.95), 1) if lat else None,
        "fallback_requests": sum(1 for r in rows if float(r.get("fallback_count") or 0) > 0),
        "truncated_by_length": sum(1 for r in rows if r.get("finish_reason") == "length"),
        "samples": {
            "input_tokens": len(ins), "output_tokens": len(outs),
            "cost": len(costs), "latency": len(lat), "usage_missing": total - len(ins),
        },
        "finish_reasons": _value_counts(rows, lambda r: r.get("finish_reason") or "(not captured)"),
        "models": _value_counts(rows, lambda r: r.get("actual_model") or "(none)"),
    }


GROUP_DIMENSIONS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "agent": lambda r: r.get("agent") or "(unattributed)",
    "tier": lambda r: r.get("selected_tier") or "(none)",
    "model": lambda r: r.get("actual_model") or "(none)",
    "band": lambda r: _input_band(r.get("input_tokens")),
    "tools": lambda r: "with tools" if _is_true(r.get("request_has_tools")) else "no tools",
    "finish": lambda r: r.get("finish_reason") or "(not captured)",
    "routing": lambda r: r.get("routing_reason") or "(none)",
    "task_type": lambda r: r.get("task_type") or "(never sent)",
}


def _split_legacy(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`routing_automatic` is NULL only on rows written before tier attribution existed."""
    attributed = [row for row in rows if row.get("routing_automatic") is not None]
    return attributed, [row for row in rows if row.get("routing_automatic") is None]


GROUPING_NOTES = (
    "task_type is read from an x-task-type header that OpenClaw has never sent, so that "
    "dimension is empty by construction, not by filter.",
    "(unattributed) holds two different things: rows predating agent attribution, and live "
    "calls on the unsuffixed agents.defaults.model.primary. The legacy filter removes the former only.",
    "usage_missing counts rows where upstream sent no token usage; means over a group with a "
    "large usage_missing are computed on that group's survivors, not on all its requests.",
)


def build_groupings(scope: str, recent: int, by: str = "agent", start: str | None = None,
                    end: str | None = None, snapshot_name: str | None = None,
                    include_legacy: bool = False) -> dict[str, Any]:
    if by not in GROUP_DIMENSIONS:
        raise HTTPException(status_code=422, detail=f"unknown grouping; use one of {sorted(GROUP_DIMENSIONS)}")
    rows, window = read_scoped_rows(scope, recent, start, end, snapshot_name, GROUPING_COLUMNS)
    attributed, legacy = _split_legacy(rows)
    selected = rows if include_legacy else attributed
    window_cost = sum(float(row.get("estimated_cost") or 0) for row in selected)
    pick = GROUP_DIMENSIONS[by]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        buckets.setdefault(str(pick(row)), []).append(row)
    return {
        "window": window, "by": by, "dimensions": sorted(GROUP_DIMENSIONS),
        "bands": list(INPUT_BANDS),
        "groups": [{"group": name, **_aggregate(members, len(selected), window_cost)}
                   for name, members in sorted(buckets.items(), key=lambda kv: str(kv[0]))],
        "totals": _aggregate(selected, len(selected), window_cost),
        "excluded_legacy_rows": 0 if include_legacy else len(legacy),
        "notes": GROUPING_NOTES,
    }


def load_router_config() -> dict[str, Any]:
    """Read the live tier routing config. Never echoes a credential: the file stores only the
    *name* of the environment variable holding the key, and only that name is returned."""
    unavailable = {"available": False, "error": None, "tiers": {}, "upstream": {}}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**unavailable, "error": "config_file_missing"}
    except ValueError:
        return {**unavailable, "error": "config_invalid_json"}
    except OSError:
        return {**unavailable, "error": "config_unreadable"}
    if not isinstance(value, dict):
        return {**unavailable, "error": "config_root_not_object"}
    return {"available": True, "error": None,
            "tiers": value.get("tiers") if isinstance(value.get("tiers"), dict) else {},
            "upstream": value.get("openrouter") if isinstance(value.get("openrouter"), dict) else {}}


def build_config(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Configured tiers side by side with the traffic that actually used them."""
    config = load_router_config()
    window_cost = sum(float(row.get("estimated_cost") or 0) for row in rows)
    attributed, legacy = _split_legacy(rows)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in attributed:
        buckets.setdefault(str(row.get("selected_tier") or "(none)"), []).append(row)
    tiers: list[dict[str, Any]] = []
    for name, tier in config["tiers"].items():
        members = buckets.get(name, [])
        declared = [tier.get("primary"), *(tier.get("fallbacks") or [])] if isinstance(tier, dict) else []
        served = [entry["name"] for entry in
                  _value_counts(members, lambda r: r.get("actual_model") or "(none)")]
        tiers.append({
            "tier": name,
            "provider": tier.get("provider") if isinstance(tier, dict) else None,
            "primary": tier.get("primary") if isinstance(tier, dict) else None,
            "fallbacks": list(tier.get("fallbacks") or []) if isinstance(tier, dict) else [],
            # The governance floor the router injects upstream (e.g. {"zdr": true}). Surfaced so a
            # missing or mistyped policy is visible on the page instead of silently unenforced.
            "provider_policy": (tier.get("provider_policy")
                                if isinstance(tier, dict) and isinstance(tier.get("provider_policy"), dict)
                                else {}),
            "observed": _aggregate(members, len(attributed), window_cost) if members else None,
            "served_models_off_config": sorted(m for m in served if m not in declared),
        })
    gaps = sorted(t["tier"] for t in tiers
                  if isinstance(config["tiers"].get(t["tier"]), dict) and not t["provider_policy"])
    upstream = config["upstream"]
    return {
        "config_available": config["available"], "config_error": config["error"],
        "config_path": CONFIG_PATH.name,
        "tiers": tiers,
        "tiers_without_governance_policy": gaps,
        "tiers_without_traffic": sorted(t["tier"] for t in tiers if not t["observed"]),
        "traffic_without_tier": [{"tier": name, **_aggregate(members, len(attributed), window_cost)}
                                 for name, members in buckets.items() if name not in config["tiers"]],
        "upstream": {"base_url": upstream.get("base_url"),
                     "timeout_seconds": upstream.get("timeout_seconds"),
                     "api_key_env": upstream.get("api_key_env")},
        "excluded_legacy_rows": len(legacy),
        "routing_automatic_requests": sum(1 for row in attributed if _is_true(row.get("routing_automatic"))),
    }


def _unavailable(exc: BaseException) -> JSONResponse:
    return JSONResponse({"error": "dashboard_unavailable", "cause": exc.__class__.__name__}, status_code=503)


SYNTHETIC_AGENT_KEYS = ("benchmark", "verify", "smoketest", "ab-", "cand")


def _agent_kind(agent: str | None) -> str:
    """Separate real callers from probe traffic, so the drawing cannot present a benchmark
    run as if an agent were paying for it."""
    name = (agent or "").strip().lower()
    if not name:
        return "unknown"
    return "synthetic" if any(k in name for k in SYNTHETIC_AGENT_KEYS) else "caller"


def build_topology(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Callers -> tiers -> models -> upstream, with the traffic each link actually carried.

    The declared chains are emitted first so a route that is idle still shows up; a link
    carrying traffic that its tier never declared is flagged rather than hidden, because
    that is what a fallback firing looks like from the outside.
    """
    config = load_router_config()
    tiers = config["tiers"]
    base_url = str((config.get("upstream") or {}).get("base_url") or "")
    upstream_host = base_url.split("//")[-1].split("/")[0] or "openrouter"
    upstream_id = f"upstream:{upstream_host}"

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    latencies_by_pair: dict[tuple[str, str], list[float]] = {}
    errors_by_node: dict[str, list[dict[str, Any]]] = {}

    def add_node(node_id: str, kind: str, label: str, **meta: Any) -> dict[str, Any]:
        node = nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "kind": kind, "label": label, "meta": {},
                    "requests": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
                    "failures": 0, "last_seen": None}
            nodes[node_id] = node
        node["meta"].update({k: v for k, v in meta.items() if v not in (None, {}, [])})
        return node

    def add_edge(src: str, dst: str, declared: str | None = None) -> dict[str, Any]:
        edge = edges.get((src, dst))
        if edge is None:
            edge = {"from": src, "to": dst, "declared": declared, "requests": 0, "cost": 0.0,
                    "input_tokens": 0, "output_tokens": 0, "failures": 0, "fallbacks": 0,
                    "truncated": 0, "latencies": [], "last_seen": None}
            edges[(src, dst)] = edge
        if declared and edge["declared"] is None:
            edge["declared"] = declared
        return edge

    declared_chain: dict[str, set[str]] = {}
    for name, tier in tiers.items():
        if not isinstance(tier, dict):
            continue
        tier_id = f"tier:{name}"
        add_node(tier_id, "tier", name, provider_policy=tier.get("provider_policy"),
                 min_max_tokens=tier.get("min_max_tokens"))
        chain = [tier.get("primary"), *list(tier.get("fallbacks") or [])]
        declared_chain[name] = {m for m in chain if m}
        for position, model in enumerate(m for m in chain if m):
            add_node(f"model:{model}", "model", model)
            add_edge(tier_id, f"model:{model}", "primary" if position == 0 else f"fallback-{position}")
    add_node(upstream_id, "upstream", upstream_host)

    for row in rows:
        agent = (row.get("agent") or "").strip()
        tier_name = row.get("selected_tier") or "(none)"
        model = row.get("actual_model") or "(none)"
        agent_id = f"agent:{agent or '(untagged)'}"
        tier_id = f"tier:{tier_name}"
        model_id = f"model:{model}"
        add_node(agent_id, _agent_kind(agent), agent or "(untagged)")
        add_node(tier_id, "tier", tier_name,
                 **({} if tier_name in tiers else {"off_config": True}))
        add_node(model_id, "model", model,
                 **({} if model in declared_chain.get(tier_name, {model}) else {"off_chain": True}))
        stamp = row.get("timestamp")
        cost = float(row.get("estimated_cost") or 0)
        ok = _is_true(row.get("success"))
        for edge in (add_edge(agent_id, tier_id), add_edge(tier_id, model_id),
                     add_edge(model_id, upstream_id)):
            edge["requests"] += 1
            edge["cost"] += cost
            edge["input_tokens"] += int(row.get("input_tokens") or 0)
            edge["output_tokens"] += int(row.get("output_tokens") or 0)
            edge["failures"] += 0 if ok else 1
            edge["fallbacks"] += 1 if int(row.get("fallback_count") or 0) else 0
            edge["truncated"] += 1 if row.get("finish_reason") == "length" else 0
            if row.get("latency_ms") is not None:
                edge["latencies"].append(float(row["latency_ms"]))
            if stamp and (edge["last_seen"] is None or stamp > edge["last_seen"]):
                edge["last_seen"] = stamp
        for node in (nodes[agent_id], nodes[tier_id], nodes[model_id], nodes[upstream_id]):
            node["requests"] += 1
            node["cost"] += cost
            node["input_tokens"] += int(row.get("input_tokens") or 0)
            node["output_tokens"] += int(row.get("output_tokens") or 0)
            node["failures"] += 0 if ok else 1
            if stamp and (node["last_seen"] is None or stamp > node["last_seen"]):
                node["last_seen"] = stamp
        # Latency is kept per (caller, tier) pair rather than per edge: the same caller hitting
        # the same tier across many models would otherwise split its own distribution.
        if row.get("latency_ms") is not None:
            latencies_by_pair.setdefault((agent or "(untagged)", tier_name), []).append(float(row["latency_ms"]))
        if not ok:
            for nid in (agent_id, tier_id, model_id):
                errors_by_node.setdefault(nid, []).append({
                    "timestamp": stamp,
                    "http_status": row.get("http_status"),
                    "outcomes": row.get("attempt_outcomes"),
                })

    for name in tiers:
        nodes[f"tier:{name}"]["meta"]["idle"] = nodes[f"tier:{name}"]["requests"] == 0
    for nid, errs in errors_by_node.items():
        if nid in nodes:
            nodes[nid]["errors"] = errs[-5:]
    latency_cells = [
        {"agent": a, "tier": t, "samples": len(vals),
         "p50_ms": round(statistics.median(vals), 1),
         "p95_ms": round(_percentile(sorted(vals), 0.95) or 0.0, 1)}
        for (a, t), vals in sorted(latencies_by_pair.items())
    ]
    for edge in edges.values():
        edge["cost"] = round(edge["cost"], 6)
        if edge["requests"] == 0:
            edge["declared_only"] = True
        lat = sorted(edge.pop("latencies"))
        edge["p50_latency_ms"] = round(statistics.median(lat), 1) if lat else None
        p95 = _percentile(lat, 0.95)  # takes a fraction, not 0-100
        edge["p95_latency_ms"] = round(p95, 1) if p95 is not None else None

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_available": config["available"], "config_error": config["error"],
        "upstream": upstream_host,
        "nodes": sorted(nodes.values(), key=lambda n: (n["kind"], -n["requests"], n["label"])),
        "edges": sorted(edges.values(), key=lambda e: (e["from"], -e["requests"])),
        "latency": {
            "cells": latency_cells,
            "agents": sorted({c["agent"] for c in latency_cells}),
            "tiers": sorted({c["tier"] for c in latency_cells}),
        },
        "summary": {
            "requests": len(rows),
            "cost": round(sum(float(r.get("estimated_cost") or 0) for r in rows), 6),
            "callers": sum(1 for n in nodes.values() if n["kind"] == "caller"),
            "synthetic_callers": sum(1 for n in nodes.values() if n["kind"] == "synthetic"),
            "tiers": sum(1 for n in nodes.values() if n["kind"] == "tier"),
            "models": sum(1 for n in nodes.values() if n["kind"] == "model"),
            "off_config_tiers": sorted(n["label"] for n in nodes.values()
                                        if n["kind"] == "tier" and n["meta"].get("off_config")),
            "off_chain_models": sorted({n["label"] for n in nodes.values()
                                         if n["kind"] == "model" and n["meta"].get("off_chain")}),
            "ungoverned_tiers": sorted(n["label"] for n in nodes.values()
                                        if n["kind"] == "tier" and n["label"] in tiers
                                        and not (tiers.get(n["label"]) or {}).get("provider_policy")),
        },
    }


def _guarded(builder: Callable[[], dict[str, Any]]):
    try:
        return builder()
    except HTTPException:
        raise
    except Exception as exc:
        return _unavailable(exc)


@router.get("/api/dashboard/snapshots")
def dashboard_snapshots() -> Any:
    return _guarded(lambda: {"snapshots": load_snapshots()})


@router.patch("/api/dashboard/snapshots/{snapshot_name}/context")
def update_snapshot_context(snapshot_name: str, payload: dict[str, Any]) -> Any:
    """Edit descriptive snapshot metadata only; the telemetry boundary is immutable."""
    editable = ("summary", "goal", "context", "benchmark_findings", "notes")
    if not set(payload).issubset(editable):
        raise HTTPException(status_code=422, detail="only snapshot context fields may be edited")
    try:
        snapshots = load_snapshots()
        snapshot = next((item for item in snapshots if item["name"] == snapshot_name), None)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        for field, value in payload.items():
            if field in ("context", "benchmark_findings") and not isinstance(value, list):
                raise HTTPException(status_code=422, detail=f"{field} must be a list")
            if field not in ("context", "benchmark_findings") and not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{field} must be text")
            snapshot[field] = value
        stored = [{key: item.get(key) for key in (
            "name", "created_at", "after_telemetry_id", "note", "summary", "goal",
            "context", "benchmark_findings", "notes") if key in item} for item in snapshots]
        SNAPSHOTS_PATH.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        updated = next(item for item in load_snapshots() if item["name"] == snapshot_name)
        return {"snapshot": updated}
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        return _unavailable(exc)


@router.get("/api/dashboard/summary")
def dashboard_summary(recent: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                      scope: str | None = None, start: str | None = None,
                      end: str | None = None, snapshot: str | None = None) -> Any:
    selected_scope = scope or "recent"
    if scope is not None and selected_scope == "recent":
        return _guarded(lambda: build_summary(recent, selected_scope, start, end, snapshot))
    return _guarded(lambda: build_summary(recent, selected_scope, start, end, snapshot))


@router.get("/api/dashboard/recent")
def dashboard_recent(limit: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                     scope: str | None = None, start: str | None = None,
                     end: str | None = None, snapshot: str | None = None) -> Any:
    return _guarded(lambda: build_recent(limit, scope or "recent", start, end, snapshot))


@router.get("/api/dashboard/timeseries")
def dashboard_timeseries(scope: str = Query("recent"),
                         recent: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                         start: str | None = None, end: str | None = None,
                         snapshot: str | None = None) -> Any:
    return _guarded(lambda: build_timeseries(scope, recent, start, end, snapshot))


@router.get("/api/dashboard/config")
def dashboard_config(recent: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                     scope: str = Query("recent"), start: str | None = None,
                     end: str | None = None, snapshot: str | None = None) -> Any:
    """Read-only view of the tier routing config beside the traffic that used each tier."""
    def build():
        rows, _ = read_scoped_rows(scope, recent, start, end, snapshot)
        return build_config(rows)
    return _guarded(build)


@router.get("/api/dashboard/groupings")
def dashboard_groupings(scope: str = Query("last_24_hours"),
                        recent: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                        by: str = Query("agent"),
                        include_legacy: bool = Query(False),
                        start: str | None = None, end: str | None = None,
                        snapshot: str | None = None) -> Any:
    """Request-shape groupings over operational telemetry only; never reads message content."""
    return _guarded(lambda: build_groupings(scope, recent, by, start, end, snapshot, include_legacy))


@router.get("/api/dashboard/topology")
def dashboard_topology(recent: int = Query(100, ge=MIN_WINDOW, le=MAX_WINDOW),
                       scope: str = Query("last_24_hours"), start: str | None = None,
                       end: str | None = None, snapshot: str | None = None) -> Any:
    """The routing graph: which caller reached which tier, which model actually served."""
    def build():
        rows, _ = read_scoped_rows(scope, recent, start, end, snapshot)
        return build_topology(rows)
    return _guarded(build)


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> Any:
    try:
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return HTMLResponse("<!doctype html><title>dashboard unavailable</title><p>static/dashboard.html could not be read.</p>", status_code=503)


@router.get("/architecture", include_in_schema=False)
def architecture_page() -> Any:
    """Live wiring diagram. Read per request like the dashboard, so edits land without a restart."""
    try:
        return HTMLResponse((STATIC_DIR / "architecture.html").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return HTMLResponse("<!doctype html><title>architecture view unavailable</title>"
                            "<p>static/architecture.html could not be read.</p>", status_code=503)


@router.get("/dashboard/vendor/chart.umd.min.js", include_in_schema=False)
def dashboard_chart_library() -> Any:
    if not CHART_JS_PATH.exists():
        return JSONResponse({"error": "chart_library_missing"}, status_code=404)
    return FileResponse(CHART_JS_PATH, media_type="application/javascript")
