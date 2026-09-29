from __future__ import annotations

import argparse
import json
import math
import statistics
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(__file__).with_name("router_telemetry.db")
TIERS = ("fast", "balanced", "deep")


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def fmt(value: object) -> str:
    return "NULL" if value is None else str(value)


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def print_map(title: str, values: dict[object, object]) -> None:
    print(title)
    if not values:
        print("  (none)")
    for key, value in values.items():
        print(f"  {fmt(key)}: {value}")


def _get(row: object, key: str, default: object = None) -> object:
    try:
        return row[key]  # sqlite3.Row and dict both support key lookup
    except (KeyError, IndexError, TypeError):
        return default


def _is_true(value: object) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.lower() in ("1", "true", "yes"))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _counter_percent(counter: Counter, key: object, total: int) -> float:
    return round(counter[key] * 100 / total, 2) if total else 0.0


def summarize_routing_rows(rows: list[object]) -> dict[str, object]:
    """Summarize already-windowed telemetry rows without reading content fields."""
    total = len(rows)
    successes = sum(_is_true(_get(row, "success")) for row in rows)
    tier_counts = Counter(_get(row, "selected_tier") or "unknown" for row in rows)
    automatic = sum(_is_true(_get(row, "routing_automatic")) for row in rows)
    explicit = sum(_get(row, "routing_automatic") is not None and not _is_true(_get(row, "routing_automatic")) for row in rows)
    unknown_routing = total - automatic - explicit
    reasons = Counter(_get(row, "routing_reason") or "unknown" for row in rows)
    requested = Counter(_get(row, "requested_tier") or "not_recorded" for row in rows)
    mismatches = [row for row in rows
                  if _get(row, "requested_tier") not in (None, "", "auto")
                  and _get(row, "selected_tier") is not None
                  and _get(row, "requested_tier") != _get(row, "selected_tier")]

    by_tier: dict[str, object] = {}
    for tier in TIERS:
        tier_rows = [row for row in rows if _get(row, "selected_tier") == tier]
        n = len(tier_rows)
        latencies = [float(_get(row, "latency_ms") or 0) for row in tier_rows if _get(row, "latency_ms") is not None]
        costs = [float(_get(row, "estimated_cost")) for row in tier_rows if _get(row, "estimated_cost") is not None]
        fallback_requests = sum(float(_get(row, "fallback_count") or 0) > 0 for row in tier_rows)
        exhausted = sum(_is_true(_get(row, "budget_exhausted")) for row in tier_rows)
        failed = sum(not _is_true(_get(row, "success")) for row in tier_rows)
        by_tier[tier] = {
            "requests": n,
            "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "median_latency_ms": round(statistics.median(latencies), 2) if latencies else None,
            "p95_latency_ms": round(_percentile(latencies, 0.95), 2) if latencies else None,
            "total_cost": round(sum(costs), 8),
            "average_cost_per_request": round(sum(costs) / n, 8) if n else None,
            "input_tokens": sum(int(_get(row, "input_tokens") or 0) for row in tier_rows),
            "output_tokens": sum(int(_get(row, "output_tokens") or 0) for row in tier_rows),
            "fallback_requests": fallback_requests,
            "fallback_rate_percent": _counter_percent(Counter({True: fallback_requests}), True, n),
            "budget_exhausted_requests": exhausted,
            "budget_exhaustion_rate_percent": _counter_percent(Counter({True: exhausted}), True, n),
            "errors": failed,
            "error_rate_percent": _counter_percent(Counter({True: failed}), True, n),
            "finish_reason_distribution": dict(Counter(_get(row, "finish_reason") or "unknown" for row in tier_rows)),
        }

    models = Counter(_get(row, "actual_model") or "unknown" for row in rows)
    sequences: Counter[str] = Counter()
    for row in rows:
        raw = _get(row, "attempted_models")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, list) and all(isinstance(model, str) for model in parsed):
            label = " -> ".join(parsed) if parsed else "(no attempted model recorded)"
        else:
            label = "(attempt sequence unavailable)"
        sequences[label] += 1

    balanced_reasons = Counter(_get(row, "routing_reason") or "unknown" for row in rows if _get(row, "selected_tier") == "balanced")
    dominant_reason, dominant_count = balanced_reasons.most_common(1)[0] if balanced_reasons else (None, 0)
    explicit_by_selected = Counter(_get(row, "selected_tier") or "unknown" for row in rows if _get(row, "routing_automatic") is not None and not _is_true(_get(row, "routing_automatic")))
    return {
        "total_requests": total,
        "successes": successes,
        "success_rate_percent": round(successes * 100 / total, 2) if total else 0.0,
        "routing_tier_counts": {tier: {"count": tier_counts[tier], "percent": _counter_percent(tier_counts, tier, total)} for tier in (*TIERS, "unknown")},
        "automatic_vs_explicit": {"automatic": automatic, "explicit": explicit, "unknown_or_legacy": unknown_routing},
        "routing_reason_distribution": dict(reasons),
        "requested_tier_distribution": dict(requested),
        "requested_tier_mismatches": {"count": len(mismatches), "pairs": dict(Counter(f"{_get(row, 'requested_tier')} -> {_get(row, 'selected_tier')}" for row in mismatches))},
        "explicit_selections_by_tier": dict(explicit_by_selected),
        "tier_operations": by_tier,
        "actual_model_distribution": dict(models),
        "attempted_model_sequences": dict(sequences),
        "balanced_reason_diagnostics": {
            "counts": dict(balanced_reasons),
            "dominant_reason": dominant_reason,
            "dominant_reason_percent": round(dominant_count * 100 / sum(balanced_reasons.values()), 2) if balanced_reasons else 0.0,
            "concentration_flag": bool(balanced_reasons and dominant_count / sum(balanced_reasons.values()) >= 0.60),
            "concentration_threshold_percent": 60,
        },
        "interpretation_note": "Explicit selections are observable, but the automatic counterfactual for an explicit request cannot be determined without reclassifying request content; content is intentionally not inspected or reported.",
    }


def routing_report(recent: int = 100) -> dict[str, object]:
    if recent <= 0:
        raise ValueError("--recent must be a positive integer")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Telemetry database does not exist: {DB_PATH}")
    with connect_read_only(DB_PATH) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'").fetchone()
        if not table:
            return summarize_routing_rows([])
        rows = db.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (recent,)).fetchall()
    result = summarize_routing_rows(list(reversed(rows)))
    result["window"] = {"requested": recent, "returned": len(rows), "ordering": "most recent telemetry id first, summarized oldest-to-newest within window"}
    return result


def print_routing_report(recent: int = 100) -> None:
    report = routing_report(recent)
    print(f"Recent routing report ({report['window']['returned']}/{recent} requests)")
    print(f"Total requests: {report['total_requests']}")
    print(f"Success rate: {report['success_rate_percent']:.2f}% ({report['successes']}/{report['total_requests']})")
    print("Routing tiers:")
    for tier, values in report["routing_tier_counts"].items():
        print(f"  {tier}: {values['count']} ({values['percent']:.2f}%)")
    print(f"Automatic vs explicit: {report['automatic_vs_explicit']}")
    print(f"Routing reasons: {report['routing_reason_distribution']}")
    print(f"Requested tiers: {report['requested_tier_distribution']}")
    print(f"Requested-tier mismatches: {report['requested_tier_mismatches']}")
    print(f"Explicit selections by tier: {report['explicit_selections_by_tier']}")
    print("Operations by tier:")
    for tier, values in report["tier_operations"].items():
        print(f"  {tier}: {values}")
    print(f"Actual models: {report['actual_model_distribution']}")
    print(f"Attempt sequences: {report['attempted_model_sequences']}")
    print(f"BALANCED reason diagnostics: {report['balanced_reason_diagnostics']}")
    print(report["interpretation_note"])


def _window_metrics(rows: list[object]) -> dict[str, object]:
    """Decision-relevant numbers for one window, for differencing against another."""
    n = len(rows)
    costs = [float(_get(r, "estimated_cost") or 0) for r in rows]
    latencies = [float(_get(r, "latency_ms") or 0) for r in rows if _get(r, "latency_ms") is not None]
    inputs = [int(_get(r, "input_tokens") or 0) for r in rows]
    successes = sum(_is_true(_get(r, "success")) for r in rows)
    return {
        "requests": n,
        "success_rate_percent": round(successes * 100 / n, 2) if n else 0.0,
        "total_cost": round(sum(costs), 8),
        "cost_per_request": round(sum(costs) / n, 8) if n else None,
        "median_latency_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 2) if latencies else None,
        "input_tokens": sum(inputs),
        "input_tokens_per_request": round(sum(inputs) / n, 1) if n else None,
        "fallback_requests": sum(float(_get(r, "fallback_count") or 0) > 0 for r in rows),
        "tier_counts": dict(Counter(_get(r, "selected_tier") or "unknown" for r in rows)),
    }


def _delta(current: object, previous: object) -> str:
    if current is None or previous is None:
        return "n/a"
    c, p = float(current), float(previous)
    if p == 0:
        return "n/a (prev 0)"
    return f"{(c - p) / p * 100:+.1f}%"


def compare_routing_windows(recent: int = 100) -> dict[str, object]:
    """Summarize the newest N requests against the N before them."""
    if recent <= 0:
        raise ValueError("--recent must be a positive integer")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Telemetry database does not exist: {DB_PATH}")
    with connect_read_only(DB_PATH) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'").fetchone():
            raise ValueError("Database is empty: telemetry table is not present")
        fetched = db.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (recent * 2,)).fetchall()
    rows = list(reversed(fetched))  # oldest first, so the newest window is the tail
    note = None
    if len(rows) < recent * 2:
        half = len(rows) // 2
        previous_rows, current_rows = rows[:half], rows[half:]
        note = f"only {len(rows)} rows available for two {recent}-request windows; split evenly"
    else:
        previous_rows, current_rows = rows[:recent], rows[recent:]
    previous, current = _window_metrics(previous_rows), _window_metrics(current_rows)
    return {
        "window_size": recent,
        "previous": previous,
        "current": current,
        "note": note,
        "deltas": {
            "success_rate_points": round(current["success_rate_percent"] - previous["success_rate_percent"], 2),
            "cost_per_request": _delta(current["cost_per_request"], previous["cost_per_request"]),
            "total_cost": _delta(current["total_cost"], previous["total_cost"]),
            "p95_latency": _delta(current["p95_latency_ms"], previous["p95_latency_ms"]),
            "median_latency": _delta(current["median_latency_ms"], previous["median_latency_ms"]),
            "input_tokens_per_request": _delta(current["input_tokens_per_request"], previous["input_tokens_per_request"]),
        },
    }


def print_window_comparison(recent: int = 100) -> None:
    report = compare_routing_windows(recent)
    prev, cur = report["previous"], report["current"]
    print(f"Window comparison: newest {cur['requests']} vs preceding {prev['requests']} requests")
    if report["note"]:
        print(f"  note: {report['note']}")
    print(f"  {'metric':<26}{'previous':>14}{'current':>14}{'delta':>14}")
    d = report["deltas"]
    rows = (
        ("success rate %", prev["success_rate_percent"], cur["success_rate_percent"], f"{d['success_rate_points']:+.2f} pts"),
        ("cost per request", prev["cost_per_request"], cur["cost_per_request"], d["cost_per_request"]),
        ("total cost", prev["total_cost"], cur["total_cost"], d["total_cost"]),
        ("median latency ms", prev["median_latency_ms"], cur["median_latency_ms"], d["median_latency"]),
        ("p95 latency ms", prev["p95_latency_ms"], cur["p95_latency_ms"], d["p95_latency"]),
        ("input tokens/req", prev["input_tokens_per_request"], cur["input_tokens_per_request"], d["input_tokens_per_request"]),
        ("fallback requests", prev["fallback_requests"], cur["fallback_requests"], ""),
    )
    for label, a, b, delta in rows:
        print(f"  {label:<26}{fmt(a):>14}{fmt(b):>14}{delta:>14}")
    print(f"  tier mix previous: {prev['tier_counts']}")
    print(f"  tier mix current : {cur['tier_counts']}")


def check_integrity(recent: int = 0) -> dict[str, object]:
    """Self-reporting data-quality audit. Any non-empty `findings` means a metric derived
    from this table is computed over incomplete data and should be quoted with care."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Telemetry database does not exist: {DB_PATH}")
    scope, params = ("", ())
    if recent > 0:
        scope = "WHERE id > (SELECT COALESCE(MAX(id),0) - ? FROM telemetry)"
        params = (recent,)
    with connect_read_only(DB_PATH) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'").fetchone():
            raise ValueError("Database is empty: telemetry table is not present")
        total = db.execute(f"SELECT COUNT(*) FROM telemetry {scope}".strip(), params).fetchone()[0]

        def count(where: str) -> int:
            sql = "SELECT COUNT(*) FROM telemetry"
            sql += f" {scope} AND ({where})" if scope else f" WHERE ({where})"
            return db.execute(sql, params).fetchone()[0]

        blind = count("success = 1 AND (input_tokens IS NULL OR estimated_cost IS NULL)")
        blind_stream = count("success = 1 AND input_tokens IS NULL AND request_has_stream = 1"
                             " AND routing_reason IS NOT NULL")
        # Rows predating routing_reason (or written by the benchmark harness) come from a
        # different code path; counting them makes the live blind rate look far worse.
        blind_legacy = count("success = 1 AND input_tokens IS NULL AND routing_reason IS NULL")
        blind_live = blind - blind_legacy
        no_agent = count("agent IS NULL")
        no_session = count("session_id IS NULL")
        no_finish = count("success = 1 AND finish_reason IS NULL")
        ok_but_error = count("success = 1 AND http_status >= 400")
        zero_but_200 = count("success = 0 AND http_status = 200")
        dupes = db.execute(
            f"SELECT COUNT(*) FROM (SELECT request_id FROM telemetry {scope} "
            f"GROUP BY request_id HAVING COUNT(*) > 1)".strip(), params).fetchone()[0]
        burst_sql = "SELECT substr(timestamp,1,13) hr, COUNT(*) n FROM telemetry"
        burst_where = "success = 1 AND input_tokens IS NULL AND routing_reason IS NOT NULL"
        burst_sql += f" {scope} AND ({burst_where})" if scope else f" WHERE ({burst_where})"
        burst_sql += " GROUP BY hr ORDER BY n DESC LIMIT 3"
        burst = db.execute(burst_sql, params).fetchall()

    findings: list[str] = []
    if blind_live:
        findings.append(
            f"{blind_live}/{total} current-path rows succeeded but record no tokens or cost "
            f"({blind_stream} streamed) - cost and token aggregates are undercounts"
            if total else "successful rows missing usage recorded")
    if ok_but_error:
        findings.append(f"{ok_but_error} rows marked success but returned HTTP >= 400 - mislabelled")
    if dupes:
        findings.append(f"{dupes} request_ids appear more than once - double-counting risk")
    warnings: list[str] = []
    if no_session and total:
        warnings.append(f"session_id NULL on {no_session}/{total} - no per-conversation attribution")
    if no_agent and total:
        warnings.append(f"agent NULL on {no_agent}/{total} - rows not attributable to a caller")
    if no_finish and total:
        warnings.append(f"finish_reason NULL on {no_finish}/{total} successful - truncation undetectable")
    if zero_but_200:
        warnings.append(f"{zero_but_200} rows failed despite HTTP 200 (stream breaks) - 'failure' is a judgement here")
    if blind_legacy:
        warnings.append(f"{blind_legacy} blind rows are legacy/other-code-path (routing_reason NULL) - excluded from findings")
    return {
        "scope_rows": total,
        "blind_live": blind_live,
        "blind_legacy": blind_legacy,
        "findings": findings,
        "warnings": warnings,
        "blind_spot_clusters": [{"hour": b["hr"], "rows": b["n"]} for b in burst],
        "verdict": "UNRELIABLE" if findings else "OK",
    }


def print_integrity(recent: int = 0) -> None:
    rep = check_integrity(recent)
    print(f"Telemetry integrity audit ({rep['scope_rows']} rows)")
    print(f"  verdict: {rep['verdict']}")
    if rep["findings"]:
        print("  FINDINGS (invalidates derived metrics):")
        for f in rep["findings"]:
            print(f"    ! {f}")
    if rep["blind_spot_clusters"]:
        print("  blind-spot concentration:")
        for c in rep["blind_spot_clusters"]:
            print(f"    {c['hour']}Z  {c['rows']} rows")
    if rep["warnings"]:
        print("  WARNINGS (coverage gaps, not corruption):")
        for w in rep["warnings"]:
            print(f"    - {w}")
    if not rep["findings"] and not rep["warnings"]:
        print("  no blind spots or coverage gaps detected")


def main() -> None:
    print(f"Database: {DB_PATH}")
    if not DB_PATH.exists():
        print("Database is empty or does not exist.")
        return

    try:
        db = connect_read_only(DB_PATH)
    except sqlite3.Error as exc:
        print(f"Unable to open database read-only: {exc}")
        return

    with db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry'"
        ).fetchone()
        if not table:
            print("Database is empty: telemetry table is not present.")
            return

        rows = db.execute("SELECT * FROM telemetry ORDER BY id").fetchall()
        if not rows:
            print("Database is empty: no telemetry requests.")
            return

        total = len(rows)
        successes = sum(int(row["success"] or 0) for row in rows)
        total_input = sum(row["input_tokens"] or 0 for row in rows)
        total_output = sum(row["output_tokens"] or 0 for row in rows)
        total_cost = sum(row["estimated_cost"] or 0 for row in rows)
        total_fallbacks = sum(row["fallback_count"] or 0 for row in rows)

        by_tier = Counter(row["selected_tier"] for row in rows)
        by_model = Counter(row["actual_model"] for row in rows)
        latency_tier = defaultdict(list)
        latency_model = defaultdict(list)
        cost_tier = defaultdict(list)
        by_hour = Counter()
        populated = {"agent": Counter(), "project": Counter(), "task_type": Counter()}

        for row in rows:
            latency_tier[row["selected_tier"]].append(row["latency_ms"] or 0)
            latency_model[row["actual_model"]].append(row["latency_ms"] or 0)
            if row["estimated_cost"] is not None:
                cost_tier[row["selected_tier"]].append(row["estimated_cost"])
            timestamp = row["timestamp"] or ""
            if len(timestamp) >= 13 and timestamp[11:13].isdigit():
                by_hour[int(timestamp[11:13])] += 1
            for field in populated:
                if row[field] is not None:
                    populated[field][row[field]] += 1

        avg_tier = {k: round(sum(v) / len(v), 2) for k, v in latency_tier.items()}
        avg_model = {k: round(sum(v) / len(v), 2) for k, v in latency_model.items()}
        avg_cost = {k: round(sum(v) / len(v), 8) for k, v in cost_tier.items()}

        print(f"Total requests: {total}")
        print(f"Success rate: {pct(successes / total)}")
        print_map("Requests by tier:", dict(by_tier))
        print_map("Requests by model:", dict(by_model))
        print_map("Average latency by tier (ms):", avg_tier)
        print_map("Average latency by model (ms):", avg_model)
        print(f"Total input tokens: {total_input}")
        print(f"Total output tokens: {total_output}")
        print(f"Total estimated cost: {total_cost:.8f}")
        print_map("Average cost per request by tier:", avg_cost)
        print(f"Total fallbacks: {total_fallbacks}")
        print_map("Requests by hour of day:", {f"{h:02d}:00": by_hour[h] for h in sorted(by_hour)})
        for field, counts in populated.items():
            print_map(f"Populated {field}:", dict(counts))

        fields = [
            "timestamp", "selected_tier", "actual_model", "http_status", "success",
            "latency_ms", "input_tokens", "output_tokens", "estimated_cost",
            "fallback_count", "request_has_stream", "agent", "project", "task_type",
        ]
        print("20 most recent requests:")
        for row in reversed(rows[-20:]):
            print("  " + " | ".join(f"{field}={fmt(row[field])}" for field in fields))

        failures = [row for row in rows if not int(row["success"] or 0)]
        print(f"\nTotal failures: {len(failures)}")
        failure_dimensions = {
            "Failures by HTTP status": "http_status",
            "Failures by exception_class": "exception_class",
            "Failures by tier": "selected_tier",
            "Failures by actual_model": "actual_model",
            "Failures by stream=true/false": "request_has_stream",
            "Failures by upstream_content_type": "upstream_content_type",
        }
        for title, field in failure_dimensions.items():
            print_map(title + ":", dict(Counter(row[field] for row in failures)))

        failure_hours = Counter()
        for row in failures:
            timestamp = row["timestamp"] or ""
            if len(timestamp) >= 13 and timestamp[11:13].isdigit():
                failure_hours[int(timestamp[11:13])] += 1
        print_map("Failures by hour of day:", {f"{h:02d}:00": failure_hours[h] for h in sorted(failure_hours)})

        print("20 most recent failed requests:")
        failure_fields = [
            "timestamp", "selected_tier", "actual_model", "http_status", "latency_ms",
            "fallback_count", "request_has_stream", "upstream_content_type", "exception_class",
        ]
        for row in reversed(failures[-20:]):
            print("  " + " | ".join(f"{field}={fmt(row[field])}" for field in failure_fields))

        if failures:
            parsed_times = []
            for row in rows:
                try:
                    parsed_times.append(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")))
                except (AttributeError, ValueError):
                    pass
            latest = max(parsed_times) if parsed_times else None
            recent_cutoff = latest - timedelta(hours=24) if latest else None
            recent_failures = []
            if recent_cutoff:
                for row in failures:
                    try:
                        when = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                        if when >= recent_cutoff:
                            recent_failures.append(row)
                    except (AttributeError, ValueError):
                        pass
            older = len(failures) - len(recent_failures)
            classification = "recent operational failures" if len(recent_failures) >= older else "historical setup/test failures"
            print("Failure recency analysis:")
            print(f"  Latest telemetry timestamp: {fmt(latest.isoformat() if latest else None)}")
            print(f"  Failures in latest 24 hours: {len(recent_failures)}")
            print(f"  Older failures: {older}")
            print(f"  Classification: mostly {classification}")
        else:
            print("Failure recency analysis: no failures recorded.")

        parsed_rows = []
        for row in rows:
            try:
                parsed_rows.append((datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")), row))
            except (AttributeError, ValueError):
                pass
        failure_times = [when for when, row in parsed_rows if not int(row["success"] or 0)]
        stable_start = None
        if failure_times:
            last_failure = max(failure_times)
            successful_after_failure = [
                (when, row) for when, row in parsed_rows
                if when > last_failure and int(row["success"] or 0)
            ]
            if successful_after_failure:
                stable_start = min(when for when, row in successful_after_failure)
        elif parsed_rows:
            successful_rows = [when for when, row in parsed_rows if int(row["success"] or 0)]
            stable_start = min(successful_rows) if successful_rows else None

        print("\nPost-fix health")
        if stable_start is None:
            print("  Stable period start: NULL (no successful request after failures)")
        else:
            stable_rows = [row for when, row in parsed_rows if when >= stable_start]
            stable_failures = [row for row in stable_rows if not int(row["success"] or 0)]
            stable_successes = sum(int(row["success"] or 0) for row in stable_rows)
            stable_latency = [row["latency_ms"] or 0 for row in stable_rows]
            stable_cost = sum(row["estimated_cost"] or 0 for row in stable_rows)
            stable_fallbacks = sum(row["fallback_count"] or 0 for row in stable_rows)
            print(f"  Stable period start: {stable_start.isoformat()}")
            print(f"  Total requests: {len(stable_rows)}")
            print(f"  Successful requests: {stable_successes}")
            print(f"  Failed requests: {len(stable_failures)}")
            print(f"  Success rate: {pct(stable_successes / len(stable_rows)) if stable_rows else '0.00%'}")
            print_map("  Failures by exception_class:", dict(Counter(row["exception_class"] for row in stable_failures)))
            print_map("  Failures by tier:", dict(Counter(row["selected_tier"] for row in stable_failures)))
            print_map("  Failures by model:", dict(Counter(row["actual_model"] for row in stable_failures)))
            print(f"  Average latency (ms): {sum(stable_latency) / len(stable_latency):.2f}")
            print(f"  Total cost: {stable_cost:.8f}")
            print(f"  Fallback count: {stable_fallbacks}")
            historical = [row for when, row in parsed_rows if when < stable_start and not int(row["success"] or 0)]
            print(f"  Historical failures including pre-fix test traffic: {len(historical)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only router telemetry reporting")
    parser.add_argument("--recent", type=int, default=100, help="most recent request count for --routing (default: 100)")
    parser.add_argument("--routing", action="store_true", help="print routing and operational summary for the recent window")
    parser.add_argument("--compare", action="store_true",
                        help="compare the newest --recent requests against the preceding --recent requests")
    parser.add_argument("--integrity", action="store_true",
                        help="audit telemetry data quality; with --recent, audit only the newest N rows")
    args = parser.parse_args()
    if args.integrity:
        try:
            print_integrity(args.recent if args.recent != 100 else 0)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.error(str(exc))
    elif args.compare:
        try:
            print_window_comparison(args.recent)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.error(str(exc))
    elif args.routing:
        try:
            print_routing_report(args.recent)
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.error(str(exc))
    else:
        main()
