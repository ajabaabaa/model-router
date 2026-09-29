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

# Columns read for aggregate reporting. Everything is operational telemetry: no body,
# no message, no token text.
SUMMARY_COLUMNS = (
    "id", "timestamp", "requested_tier", "selected_tier", "routing_automatic", "routing_reason",
    "actual_model", "attempted_models", "input_tokens", "output_tokens", "latency_ms",
    "estimated_cost", "http_status", "success", "fallback_count", "budget_exhausted",
    "finish_reason", "agent",
)

# Columns the recent-request table may show. A strict subset of SUMMARY_COLUMNS and
# nothing else, so the UI cannot render a field the API does not send.
RECENT_COLUMNS = (
    "timestamp", "requested_tier", "selected_tier", "routing_automatic", "routing_reason",
    "actual_model", "agent", "latency_ms", "estimated_cost", "http_status", "success",
    "fallback_count", "budget_exhausted",
)

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
                     end: str | None = None, snapshot_name: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one authoritative row set for both summary and recent-table APIs."""
    if scope not in SCOPE_NAMES:
        raise HTTPException(status_code=422, detail="unsupported dashboard scope")
    path = db_path()
    rows: list[dict[str, Any]] = []
    if scope == "recent":
        rows = read_window(recent, SUMMARY_COLUMNS)
        return rows, {"scope": scope, "requested": recent, "returned": len(rows),
                      "available_windows": list(WINDOWS)}
    if path.exists():
        with connect_read_only(path) as db:
            if _has_telemetry_table(db):
                rows = [dict(row) for row in db.execute(
                    f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM telemetry ORDER BY id"
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


def _unavailable(exc: BaseException) -> JSONResponse:
    return JSONResponse({"error": "dashboard_unavailable", "cause": exc.__class__.__name__}, status_code=503)


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


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> Any:
    try:
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return HTMLResponse("<!doctype html><title>dashboard unavailable</title><p>static/dashboard.html could not be read.</p>", status_code=503)


@router.get("/dashboard/vendor/chart.umd.min.js", include_in_schema=False)
def dashboard_chart_library() -> Any:
    if not CHART_JS_PATH.exists():
        return JSONResponse({"error": "chart_library_missing"}, status_code=404)
    return FileResponse(CHART_JS_PATH, media_type="application/javascript")
