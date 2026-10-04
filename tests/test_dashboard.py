"""Tests for the read-only monitoring dashboard.

The fixture telemetry table deliberately carries a `prompt_text` column holding a
sentinel value. The real router never stores content, so this proves the dashboard's
column whitelist does the protecting rather than the absence of a content column.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app
import dashboard
import telemetry_report

SENTINEL = "SENTINEL-PROMPT-CONTENT-MUST-NEVER-BE-SERVED"

BASE_ROW = {
    "request_id": "r", "selected_tier": "fast", "actual_model": "m1", "input_tokens": 1,
    "output_tokens": 2, "latency_ms": 100.0, "estimated_cost": 0.01, "http_status": 200,
    "success": 1, "fallback_count": 0, "budget_exhausted": 0, "requested_tier": None,
    "routing_reason": "default_interactive", "routing_automatic": 1,
    "attempted_models": json.dumps(["m1"]), "finish_reason": "stop", "agent": "tester",
    "request_has_tools": 0, "task_type": None,
}

FIXTURE_ROWS = [
    {"latency_ms": 100.0, "estimated_cost": 0.01},
    {"latency_ms": 200.0, "estimated_cost": 0.02, "request_has_tools": 1},
    {"latency_ms": 300.0, "estimated_cost": 0.03, "success": 0, "http_status": 502,
     "selected_tier": "balanced", "actual_model": "m3", "fallback_count": 1,
     "routing_reason": "security_sensitive", "attempted_models": json.dumps(["m2", "m3"])},
    {"latency_ms": 400.0, "estimated_cost": 0.04, "selected_tier": "balanced", "actual_model": "m4",
     "routing_automatic": 0, "requested_tier": "deep", "routing_reason": "explicit_tier",
     "budget_exhausted": 1, "http_status": 504, "attempted_models": json.dumps(["m4"]),
     "request_has_tools": 1},
    {"latency_ms": 1000.0, "estimated_cost": None, "selected_tier": "deep", "actual_model": "m1"},
]


def make_db(path, rows):
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
            " request_id TEXT, selected_tier TEXT, actual_model TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, latency_ms REAL, estimated_cost REAL, http_status INTEGER,"
            " success INTEGER, fallback_count INTEGER, budget_exhausted INTEGER,"
            " requested_tier TEXT, routing_reason TEXT, routing_automatic INTEGER,"
            " attempted_models TEXT, finish_reason TEXT, agent TEXT,"
            " request_has_tools INTEGER, task_type TEXT, prompt_text TEXT)"
        )
        for index, override in enumerate(rows, start=1):
            data = dict(BASE_ROW)
            data.update(timestamp=f"2026-09-29T00:{index:02d}:00Z", request_id=f"r{index}")
            data.update(override)
            names = ", ".join(data)
            db.execute(f"INSERT INTO telemetry ({names}, prompt_text) VALUES ({', '.join(['?'] * (len(data) + 1))})",
                       (*data.values(), SENTINEL))
    return path


def use_db(monkeypatch, path):
    monkeypatch.setattr(telemetry_report, "DB_PATH", path)
    monkeypatch.setattr(app, "DB_PATH", path)


@pytest.fixture
def client(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "telemetry.db", FIXTURE_ROWS))
    return TestClient(app.app)


def test_summary_cards_match_hand_calculated_window(client):
    cards = client.get("/api/dashboard/summary?recent=5").json()["cards"]
    assert cards["total_requests"] == 5
    assert cards["success_rate_percent"] == 80.0
    assert cards["average_latency_ms"] == 400.0
    assert cards["median_latency_ms"] == 300.0
    assert cards["p95_latency_ms"] == 1000.0
    assert cards["total_estimated_cost"] == pytest.approx(0.1)
    assert cards["average_cost_per_request"] == pytest.approx(0.02)
    assert cards["requests_with_cost"] == 4
    assert cards["fallback_requests"] == 1 and cards["fallback_rate_percent"] == 20.0
    assert cards["budget_exhausted_requests"] == 1 and cards["budget_exhaustion_rate_percent"] == 20.0


def test_summary_window_reports_requested_and_returned(client):
    window = client.get("/api/dashboard/summary?recent=3").json()["window"]
    assert window["requested"] == 3 and window["returned"] == 3
    assert window["available_windows"] == [25, 50, 100, 500]


def test_recent_window_is_honoured_for_every_selector(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "telemetry.db", [{"latency_ms": float(i)} for i in range(1, 131)]))
    client = TestClient(app.app)
    for window in (25, 50, 100, 500):
        body = client.get(f"/api/dashboard/summary?recent={window}").json()
        expected = min(window, 130)
        assert body["window"] == {"requested": window, "returned": expected, "available_windows": [25, 50, 100, 500]}
        assert body["cards"]["total_requests"] == expected
        # Rows 1..130 have latency == row number, so the newest `expected` rows average
        # the arithmetic mean of (130 - expected + 1) .. 130.
        assert body["cards"]["average_latency_ms"] == pytest.approx((261 - expected) / 2, abs=0.01)
    assert client.get("/api/dashboard/summary?recent=100").json()["cards"]["average_latency_ms"] == pytest.approx(80.5)
    limited = client.get("/api/dashboard/recent?limit=5").json()
    assert limited["window"] == {"requested": 5, "returned": 5}
    assert [row["latency_ms"] for row in limited["requests"]] == [130.0, 129.0, 128.0, 127.0, 126.0]


def test_recent_returns_newest_first_and_honours_limit(client):
    body = client.get("/api/dashboard/recent?limit=3").json()
    assert body["window"] == {"requested": 3, "returned": 3}
    stamps = [row["timestamp"] for row in body["requests"]]
    assert stamps == ["2026-09-29T00:05:00Z", "2026-09-29T00:04:00Z", "2026-09-29T00:03:00Z"]
    assert all(set(row) == set(dashboard.RECENT_COLUMNS) for row in body["requests"])


BANNED_KEYS = {
    "prompt", "prompts", "prompt_text", "content", "messages", "message", "response",
    "response_text", "answer", "completion", "output_text", "reasoning",
    "reasoning_content", "text", "choices", "tool_calls", "arguments",
}


def walk_keys(node):
    if isinstance(node, dict):
        yield from node.keys()
        for value in node.values():
            yield from walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_keys(item)


def test_prompt_and_response_content_is_never_exposed(client):
    for path in ("/api/dashboard/summary?recent=5", "/api/dashboard/recent?limit=5"):
        body = client.get(path)
        assert SENTINEL not in body.text
        assert not BANNED_KEYS & set(walk_keys(body.json()))

    rows = client.get("/api/dashboard/recent?limit=5").json()["requests"]
    assert all(set(row) <= set(dashboard.RECENT_COLUMNS) for row in rows)
    assert SENTINEL not in client.get("/dashboard").text

    # Positive control: the content column is populated, so the absence above is a
    # real filter and not an accident of empty data.
    with sqlite3.connect(telemetry_report.DB_PATH) as db:
        stored = db.execute("SELECT COUNT(prompt_text), COUNT(*) FROM telemetry").fetchone()
    assert stored == (5, 5)


def test_dashboard_queries_cannot_mutate_telemetry(client):
    db_file = telemetry_report.DB_PATH
    before = sqlite3.connect(db_file).execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(latency_ms), 0) FROM telemetry").fetchone()
    for _ in range(3):
        client.get("/api/dashboard/summary?recent=5")
        client.get("/api/dashboard/recent?limit=5")
        client.get("/dashboard")
    after = sqlite3.connect(db_file).execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(latency_ms), 0) FROM telemetry").fetchone()
    assert after == before
    assert dashboard.connect_read_only is telemetry_report.connect_read_only
    with telemetry_report.connect_read_only(db_file) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO telemetry (timestamp, latency_ms, http_status, success) VALUES ('now', 1, 200, 1)")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM telemetry")


def test_routing_section_reports_tiers_modes_and_mismatches(client):
    routing = client.get("/api/dashboard/summary?recent=5").json()["routing"]
    assert {tier: value["count"] for tier, value in routing["routing_tier_counts"].items()} == {"fast": 2, "balanced": 2, "deep": 1, "unknown": 0}
    assert routing["routing_tier_counts"]["fast"]["percent"] == 40.0
    assert routing["automatic_vs_explicit"] == {"automatic": 4, "explicit": 1, "unknown_or_legacy": 0}
    assert routing["routing_reason_distribution"] == {"default_interactive": 3, "security_sensitive": 1, "explicit_tier": 1}
    assert routing["requested_tier_mismatches"]["count"] == 1
    assert routing["requested_tier_mismatches"]["pairs"] == {"deep -> balanced": 1}
    assert routing["explicit_selections_by_tier"] == {"balanced": 1}


def test_model_section_reports_models_and_attempt_sequences(client):
    models = client.get("/api/dashboard/summary?recent=5").json()["models"]
    assert models["actual_model_distribution"] == {"m1": 3, "m3": 1, "m4": 1}
    assert models["attempted_model_sequences"] == {"m1": 3, "m2 -> m3": 1, "m4": 1}
    assert models["tier_operations"]["fast"]["p95_latency_ms"] == 200.0


def test_dashboard_api_reuses_cli_report_numbers(client):
    api = client.get("/api/dashboard/summary?recent=5").json()["routing"]
    cli = telemetry_report.routing_report(5)
    for key in ("total_requests", "success_rate_percent", "routing_tier_counts", "automatic_vs_explicit",
                "routing_reason_distribution", "requested_tier_mismatches", "actual_model_distribution",
                "attempted_model_sequences", "tier_operations"):
        assert api[key] == cli[key], key


def test_missing_database_is_handled_cleanly(monkeypatch, tmp_path):
    use_db(monkeypatch, tmp_path / "does-not-exist.db")
    client = TestClient(app.app)
    summary = client.get("/api/dashboard/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["cards"]["total_requests"] == 0 and body["cards"]["success_rate_percent"] == 0.0
    assert body["cards"]["average_latency_ms"] is None and body["cards"]["p95_latency_ms"] is None
    assert body["health"]["database_exists"] is False and body["health"]["database_available"] is False
    assert client.get("/api/dashboard/recent?limit=5").json()["requests"] == []
    assert client.get("/dashboard").status_code == 200


def test_empty_database_without_table_is_handled_cleanly(monkeypatch, tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    use_db(monkeypatch, path)
    client = TestClient(app.app)
    body = client.get("/api/dashboard/summary").json()
    assert body["cards"]["total_requests"] == 0
    assert body["health"]["database_available"] is True
    assert client.get("/api/dashboard/recent").json()["window"]["returned"] == 0


@pytest.mark.parametrize("recent", ["0", "1001", "abc"])
def test_window_parameter_is_validated(client, recent):
    assert client.get(f"/api/dashboard/summary?recent={recent}").status_code == 422
    assert client.get(f"/api/dashboard/recent?limit={recent}").status_code == 422


def test_dashboard_page_and_chart_asset_are_served(client):
    page = client.get("/dashboard")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "OpenClaw Router" in page.text
    assert "/api/dashboard/summary" in page.text and "/api/dashboard/recent" in page.text
    script = client.get("/dashboard/vendor/chart.umd.min.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert len(script.content) > 100_000


def token_chart_block(page_text):
    """The c-ts-tokens chart config plus the per-request data prep it feeds on."""
    start = page_text.index("const perRequest =")
    return page_text[start:page_text.index("const agents = Object.entries", start)]


def test_token_chart_puts_output_on_its_own_right_hand_axis(client):
    """Input runs ~70x output, so a shared linear axis renders output as a flat zero line."""
    block = token_chart_block(client.get("/dashboard").text)
    before_output, after_output = block.split('label: "output per request (right axis)"')
    assert 'yAxisID: "y"' in before_output
    assert 'yAxisID: "y1"' in after_output
    assert 'y1: {' in block and 'position: "right"' in block
    assert "drawOnChartArea: false" in block, "second axis must not overdraw the first axis' grid"


def test_token_chart_labels_both_axes_for_the_reader(client):
    page = client.get("/dashboard").text
    block = token_chart_block(page)
    assert "separate axes" in page
    assert 'text: "input / req"' in block and 'text: "output / req"' in block


def test_token_chart_divides_totals_by_request_count(client):
    """Per-bucket totals cannot separate 'more calls' from 'fatter calls'; per-request can."""
    block = token_chart_block(client.get("/dashboard").text)
    assert 'perRequest("input_tokens")' in block and 'perRequest("output_tokens")' in block
    assert "s.requests ?" in block, "must not divide by a zero-request bucket"
    assert "Tokens per request per" in client.get("/dashboard").text


def test_bucketed_charts_default_to_a_time_scope(client):
    """A request-count window yields 1-2 hourly buckets, so the per-hour charts drew nothing."""
    page = client.get("/dashboard").text
    assert '<option value="last_24_hours" selected>' in page
    assert 'value="recent_100" selected' not in page


def test_dashboard_fault_does_not_affect_model_routing(monkeypatch, tmp_path):
    path = tmp_path / "telemetry.db"
    monkeypatch.setattr(app, "DB_PATH", path)
    monkeypatch.setattr(telemetry_report, "DB_PATH", path)
    app.init_db()
    monkeypatch.setenv("OPENROUTER_KEY", "test-key")

    def boom(*args, **kwargs):
        raise RuntimeError("dashboard data source exploded")

    monkeypatch.setattr(dashboard, "read_window", boom)
    client = TestClient(app.app)
    for endpoint in ("/api/dashboard/summary", "/api/dashboard/recent"):
        response = client.get(endpoint)
        assert response.status_code == 503 and response.json()["error"] == "dashboard_unavailable"

    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    routed = client.post("/v1/chat/completions", json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert routed.status_code == 200 and routed.json()["choices"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM telemetry WHERE success = 1").fetchone()[0] == 1


def test_existing_router_endpoints_are_unchanged(client):
    assert client.get("/health").status_code == 200
    summary = client.get("/telemetry/summary").json()
    assert summary["total_requests"] == 5 and summary["fallback_count"] == 1


def scoped_rows():
    return [
        {"timestamp": "2026-09-22T15:59:00Z", "latency_ms": 10, "estimated_cost": 0.01, "selected_tier": "fast", "actual_model": "m-fast"},
        {"timestamp": "2026-09-22T16:00:00Z", "latency_ms": 20, "estimated_cost": 0.02, "selected_tier": "balanced", "actual_model": "m-balanced"},
        {"timestamp": "2026-09-28T15:59:00Z", "latency_ms": 30, "estimated_cost": 0.03, "selected_tier": "fast", "actual_model": "m-fast"},
        {"timestamp": "2026-09-28T16:00:00Z", "latency_ms": 40, "estimated_cost": 0.04, "selected_tier": "deep", "actual_model": "m-deep"},
        {"timestamp": "2026-09-29T03:59:00Z", "latency_ms": 50, "estimated_cost": 0.05, "selected_tier": "fast", "actual_model": "m-fast"},
        {"timestamp": "2026-09-29T04:00:00Z", "latency_ms": 60, "estimated_cost": 0.06, "selected_tier": "balanced", "actual_model": "m-balanced"},
        {"timestamp": "2026-09-29T15:59:00Z", "latency_ms": 70, "estimated_cost": 0.07, "selected_tier": "fast", "actual_model": "m-fast"},
        {"timestamp": "2026-09-29T16:00:00Z", "latency_ms": 80, "estimated_cost": 0.08, "selected_tier": "deep", "actual_model": "m-deep"},
    ]


def snapshot_file(path, anchor=4):
    path.write_text(json.dumps([{
        "name": "Policy test", "created_at": "2026-09-29T01:35:00-04:00",
        "after_telemetry_id": anchor, "summary": "test context", "goal": "observe",
        "context": ["item"], "benchmark_findings": ["finding"], "notes": "test note",
    }]), encoding="utf-8")
    return path


def test_today_last_24_hours_and_last_7_days_filter_by_timestamp(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "dates.db", scoped_rows()))
    monkeypatch.setattr(dashboard, "_now_local", lambda: datetime.fromisoformat("2026-09-29T12:00:00-04:00"))
    client = TestClient(app.app)
    today = client.get("/api/dashboard/summary?scope=today").json()
    last24 = client.get("/api/dashboard/summary?scope=last_24_hours").json()
    last7 = client.get("/api/dashboard/summary?scope=last_7_days").json()
    assert today["cards"]["total_requests"] == 3
    assert last24["cards"]["total_requests"] == 5
    assert last7["cards"]["total_requests"] == 7
    assert client.get("/api/dashboard/recent?scope=today").json()["window"]["returned"] == 3


def test_custom_date_range_is_inclusive_and_validated(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "custom.db", scoped_rows()))
    client = TestClient(app.app)
    url = "/api/dashboard/summary?scope=custom&start=2026-09-29T04%3A00%3A00Z&end=2026-09-29T15%3A59%3A00Z"
    body = client.get(url).json()
    assert body["cards"]["total_requests"] == 2
    assert client.get("/api/dashboard/summary?scope=custom").status_code == 422
    assert client.get("/api/dashboard/summary?scope=custom&start=2026-09-30&end=2026-09-29").status_code == 422


def test_snapshot_filter_excludes_anchor_and_applies_to_all_summary_metrics(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "snapshot.db", scoped_rows()))
    monkeypatch.setattr(dashboard, "SNAPSHOTS_PATH", snapshot_file(tmp_path / "snapshots.json", anchor=4))
    client = TestClient(app.app)
    summary = client.get("/api/dashboard/summary?scope=since_snapshot&snapshot=Policy%20test").json()
    table = client.get("/api/dashboard/recent?scope=since_snapshot&snapshot=Policy%20test").json()
    assert summary["window"]["requests_since_snapshot"] == 4
    assert summary["cards"]["total_requests"] == 4
    assert summary["cost_analytics"]["total_cost"] == pytest.approx(0.26)
    assert summary["cost_analytics"]["total_cost"] == pytest.approx(
        sum(row["estimated_cost"] for row in scoped_rows()[4:])
    )
    assert [row["timestamp"] for row in table["requests"]] == [r["timestamp"] for r in scoped_rows()[4:]][::-1]
    assert summary["window"]["snapshot"]["after_telemetry_id"] == 4


def test_custom_scope_cost_analytics_match_scoped_rows(monkeypatch, tmp_path):
    rows = scoped_rows()
    use_db(monkeypatch, make_db(tmp_path / "cost.db", rows))
    client = TestClient(app.app)
    query = "scope=custom&start=2026-09-29T04%3A00%3A00Z&end=2026-09-29T15%3A59%3A00Z"
    summary = client.get(f"/api/dashboard/summary?{query}").json()
    recent = client.get(f"/api/dashboard/recent?{query}").json()
    scoped_costs = [0.06, 0.07]
    analytics = summary["cost_analytics"]
    assert analytics["total_cost"] == pytest.approx(sum(scoped_costs))
    assert summary["cards"]["total_requests"] == len(recent["requests"]) == 2
    assert sum(x["total_cost"] for x in analytics["total_cost_by_tier"].values()) == pytest.approx(analytics["total_cost"])
    assert sum(analytics["total_cost_by_model"].values()) == pytest.approx(analytics["total_cost"])
    assert analytics["cost_over_time"][-1]["cumulative_cost"] == pytest.approx(analytics["total_cost"])
    assert analytics["total_cost_by_tier"]["balanced"]["average_cost_per_request"] == pytest.approx(0.06)


def test_switching_scope_restores_cost_and_request_values(monkeypatch, tmp_path):
    use_db(monkeypatch, make_db(tmp_path / "switch.db", scoped_rows()))
    monkeypatch.setattr(dashboard, "_now_local", lambda: datetime.fromisoformat("2026-09-29T12:00:00-04:00"))
    client = TestClient(app.app)
    today = client.get("/api/dashboard/summary?scope=today").json()
    recent = client.get("/api/dashboard/summary?recent=2").json()
    today_again = client.get("/api/dashboard/summary?scope=today").json()
    assert today["cards"]["total_requests"] == today_again["cards"]["total_requests"] == 3
    assert today["cost_analytics"]["total_cost"] == today_again["cost_analytics"]["total_cost"]
    assert recent["cards"]["total_requests"] == 2
    assert recent["cost_analytics"]["total_cost"] != today["cost_analytics"]["total_cost"]


def test_snapshot_context_edit_persists_without_changing_boundary_or_telemetry(monkeypatch, tmp_path):
    path = make_db(tmp_path / "snapshot-edit.db", scoped_rows())
    use_db(monkeypatch, path)
    monkeypatch.setattr(dashboard, "SNAPSHOTS_PATH", snapshot_file(tmp_path / "snapshots.json", anchor=4))
    client = TestClient(app.app)
    before = sqlite3.connect(path).execute("SELECT COUNT(*), MAX(id) FROM telemetry").fetchone()
    edited = client.patch("/api/dashboard/snapshots/Policy%20test/context", json={
        "summary": "updated summary", "context": ["changed context"], "notes": "edited note",
    })
    assert edited.status_code == 200
    snapshot = client.get("/api/dashboard/snapshots").json()["snapshots"][0]
    after = sqlite3.connect(path).execute("SELECT COUNT(*), MAX(id) FROM telemetry").fetchone()
    assert snapshot["summary"] == "updated summary" and snapshot["context"] == ["changed context"]
    assert snapshot["after_telemetry_id"] == 4
    assert before == after
    assert client.get("/api/dashboard/summary?scope=since_snapshot&snapshot=Policy%20test").json()["window"]["snapshot"]["after_telemetry_id"] == 4
    assert client.get("/api/dashboard/summary?scope=since_snapshot&snapshot=Policy%20test").json()["window"]["snapshot"]["summary"] == "updated summary"
    stored_text = " ".join(str(value) for row in sqlite3.connect(path).execute("SELECT * FROM telemetry") for value in row)
    assert "updated summary" not in stored_text and "changed context" not in stored_text


def test_snapshot_scope_rejects_unknown_snapshot_and_snapshot_list_is_metadata_only(client):
    assert client.get("/api/dashboard/summary?scope=since_snapshot&snapshot=missing").status_code == 422
    snapshots = client.get("/api/dashboard/snapshots").json()["snapshots"]
    assert isinstance(snapshots, list)
    page = client.get("/dashboard").text
    assert "Snapshot Context" in page and "edit-context" in page and "benchmark_findings" in page


def test_timeseries_buckets_agents_and_single_day_projection_unavailable(client):
    body = client.get("/api/dashboard/timeseries?scope=recent&recent=5").json()
    # All fixture rows sit inside one hour, so one hour bucket and one day bucket.
    assert body["bucket"] == "hour"
    assert len(body["series"]) == 1
    bucket = body["series"][0]
    assert bucket["requests"] == 5 and bucket["ok"] == 4 and bucket["failed"] == 1
    assert bucket["cost"] == pytest.approx(0.1)
    assert body["agents"]["tester"]["requests"] == 5
    assert body["projection"]["available"] is False
    assert body["projection"]["days_observed"] == 1


def test_timeseries_projection_flags_unstable_linear_fit(monkeypatch, tmp_path):
    # Dates are relative to now because the endpoint scopes by a rolling window: hard-coded
    # timestamps silently fell out of `last_7_days`. They are also all in the *past* - a fixture
    # hour later than the current UTC hour gets dropped by the window's upper bound.
    def at(days_ago, hour):
        day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        return f"{day}T{hour:02d}:00:00Z"

    rows = [
        {"timestamp": at(3, 12), "estimated_cost": 0.01, "agent": "a"},
        {"timestamp": at(3, 13), "estimated_cost": 0.01, "agent": "a"},
        {"timestamp": at(2, 12), "estimated_cost": 0.01, "agent": "b"},
        {"timestamp": at(2, 13), "estimated_cost": 0.01, "agent": "b"},
        {"timestamp": at(1, 12), "estimated_cost": 5.00, "agent": "a"},
        {"timestamp": at(1, 13), "estimated_cost": 5.00, "agent": "a"},
    ]
    use_db(monkeypatch, make_db(tmp_path / "tel.db", rows))
    client = TestClient(app.app)
    body = client.get("/api/dashboard/timeseries?scope=last_7_days").json()
    assert body["bucket"] == "hour"
    assert len(body["days"]) == 3
    proj = body["projection"]
    assert proj["available"] is True and proj["confidence"] == "low"
    # Three days with a 500x jump on the last one must be flagged, not presented as fact.
    assert proj["cost"]["divergence_ratio_30d"] > 3
    assert any("unstable" in w for w in proj["warnings"]), proj["warnings"]
    assert any("only 3 day buckets" in w for w in proj["warnings"])
    # Agent attribution survives into the timeseries payload.
    assert set(body["agents"]) == {"a", "b"}
    assert body["agents"]["a"]["cost"] == pytest.approx(10.02)


def test_recent_table_exposes_agent_column(client):
    requests = client.get("/api/dashboard/recent?limit=5").json()["requests"]
    assert all("agent" in row for row in requests)
    assert requests[0]["agent"] == "tester"
    assert "prompt_text" not in requests[0]


TEST_CONFIG = {
    "tiers": {
        "fast": {"provider": "openrouter", "primary": "m1", "fallbacks": ["m2"],
                 "provider_policy": {"zdr": True}},
        # m4 actually served balanced traffic but is not declared here, so the panel must
        # surface it: this is how a hand-edited config drifts away from what runs.
        "balanced": {"provider": "openrouter", "primary": "m3", "fallbacks": []},
        "unused": {"provider": "openrouter", "primary": "m9", "fallbacks": []},
    },
    "openrouter": {
        "base_url": "https://example.test/api/v1", "timeout_seconds": 120,
        "api_key_env": "ROUTER_TEST_KEY", "api_key": "sk-LEAK-CHECK-MUST-NEVER-BE-SERVED",
    },
}


def tiers_named(entries):
    return {entry["tier"] for entry in entries}


def config_client(monkeypatch, tmp_path, client):
    config_path = tmp_path / "router_config.json"
    config_path.write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
    monkeypatch.setattr(dashboard, "CONFIG_PATH", config_path)
    return client.get("/api/dashboard/config?scope=recent&recent=5").json()


def test_config_panel_pairs_each_tier_with_the_traffic_that_used_it(monkeypatch, tmp_path, client):
    cfg = config_client(monkeypatch, tmp_path, client)
    assert cfg["config_available"] is True and cfg["config_error"] is None
    tiers = {t["tier"]: t for t in cfg["tiers"]}
    assert tiers["fast"]["primary"] == "m1"
    assert tiers["fast"]["observed"]["requests"] == 2
    assert tiers["fast"]["observed"]["cost_total"] == pytest.approx(0.03)
    assert tiers["balanced"]["observed"]["requests"] == 2


def test_config_panel_names_a_configured_tier_that_no_traffic_ever_used(monkeypatch, tmp_path, client):
    cfg = config_client(monkeypatch, tmp_path, client)
    assert cfg["tiers_without_traffic"] == ["unused"]
    assert tiers_named(cfg["tiers"]) == {"fast", "balanced", "unused"}


def test_config_panel_surfaces_models_that_served_traffic_off_config(monkeypatch, tmp_path, client):
    cfg = config_client(monkeypatch, tmp_path, client)
    tiers = {t["tier"]: t for t in cfg["tiers"]}
    assert tiers["balanced"]["served_models_off_config"] == ["m4"]
    assert tiers["fast"]["served_models_off_config"] == []


def test_config_panel_shows_each_tiers_governance_floor_and_names_the_gaps(monkeypatch, tmp_path, client):
    """A tier without a policy is silently un-governed; the panel must say so rather than
    let the column read blank."""
    cfg = config_client(monkeypatch, tmp_path, client)
    tiers = {t["tier"]: t for t in cfg["tiers"]}
    assert tiers["fast"]["provider_policy"] == {"zdr": True}
    assert tiers["balanced"]["provider_policy"] == {}
    assert cfg["tiers_without_governance_policy"] == ["balanced", "unused"]


def test_config_panel_reports_traffic_on_a_tier_that_is_not_configured(monkeypatch, tmp_path, client):
    cfg = config_client(monkeypatch, tmp_path, client)
    assert [t["tier"] for t in cfg["traffic_without_tier"]] == ["deep"]
    assert cfg["traffic_without_tier"][0]["requests"] == 1


def test_config_panel_reports_how_much_traffic_was_actually_auto_routed(monkeypatch, tmp_path, client):
    cfg = config_client(monkeypatch, tmp_path, client)
    # Rows 1,2,3,5 carry routing_automatic=1; row 4 is an explicit tier request.
    assert cfg["routing_automatic_requests"] == 4
    assert cfg["excluded_legacy_rows"] == 0


def test_config_panel_never_returns_a_credential_even_when_the_file_holds_one(monkeypatch, tmp_path, client):
    """The real file stores only the *name* of the key's env var; a planted value must not appear."""
    cfg = config_client(monkeypatch, tmp_path, client)
    assert set(cfg["upstream"]) == {"base_url", "timeout_seconds", "api_key_env"}
    assert cfg["upstream"]["api_key_env"] == "ROUTER_TEST_KEY"
    assert "sk-LEAK-CHECK-MUST-NEVER-BE-SERVED" not in client.get(
        "/api/dashboard/config?scope=recent&recent=5").text
    assert not {"api_key", "key", "secret", "token", "authorization"} & set(walk_keys(cfg))


def test_config_panel_degrades_when_the_config_file_is_absent(monkeypatch, tmp_path, client):
    monkeypatch.setattr(dashboard, "CONFIG_PATH", tmp_path / "absent.json")
    body = client.get("/api/dashboard/config?scope=recent&recent=5")
    assert body.status_code == 200
    assert body.json()["config_available"] is False
    assert body.json()["config_error"] == "config_file_missing"
    assert body.json()["tiers"] == []


def test_config_panel_reports_which_tiers_ran_when_the_config_is_unparseable(monkeypatch, tmp_path, client):
    """A half-written config must still show the traffic, with the breakage named."""
    broken = tmp_path / "router_config.json"
    broken.write_text('{"tiers": {"fast": ', encoding="utf-8")
    monkeypatch.setattr(dashboard, "CONFIG_PATH", broken)
    cfg = client.get("/api/dashboard/config?scope=recent&recent=5").json()
    assert cfg["config_available"] is False and cfg["config_error"] == "config_invalid_json"
    assert sorted(t["tier"] for t in cfg["traffic_without_tier"]) == ["balanced", "deep", "fast"]


def test_groupings_exclude_pre_attribution_rows_unless_asked(monkeypatch, tmp_path):
    path = make_db(tmp_path / "legacy.db", [
        {"agent": "live", "routing_automatic": 0, "estimated_cost": 1.0},
        {"agent": None, "routing_automatic": None, "estimated_cost": 99.0},
    ])
    use_db(monkeypatch, path)
    scoped = TestClient(app.app)
    default = scoped.get("/api/dashboard/groupings?scope=recent&recent=10&by=agent").json()
    assert default["excluded_legacy_rows"] == 1
    assert [g["group"] for g in default["groups"]] == ["live"]
    assert default["totals"]["cost_total"] == pytest.approx(1.0)

    widened = scoped.get(
        "/api/dashboard/groupings?scope=recent&recent=10&by=agent&include_legacy=true").json()
    assert widened["excluded_legacy_rows"] == 0
    assert [g["group"] for g in widened["groups"]] == ["(unattributed)", "live"]
    assert widened["totals"]["cost_total"] == pytest.approx(100.0)


def test_groupings_reject_an_unknown_dimension(client):
    assert client.get("/api/dashboard/groupings?by=prompt_text").status_code == 422


def test_groupings_name_the_sample_behind_every_average(client):
    """A mean over the rows that captured usage must not read as a mean over all requests."""
    groups = client.get("/api/dashboard/groupings?scope=recent&recent=5&by=tier").json()["groups"]
    deep = next(g for g in groups if g["group"] == "deep")
    assert deep["requests"] == 1
    assert deep["samples"]["cost"] == 0 and deep["cost_avg"] is None
    assert deep["samples"]["input_tokens"] == 1 and deep["samples"]["usage_missing"] == 0


def test_grouping_by_tools_splits_on_request_structure_not_content(client):
    groups = client.get("/api/dashboard/groupings?scope=recent&recent=5&by=tools").json()["groups"]
    assert {g["group"]: g["requests"] for g in groups} == {"no tools": 3, "with tools": 2}
    assert next(g for g in groups if g["group"] == "with tools")["input_tokens_avg"] == 1


def test_group_labels_are_values_never_object_keys(client):
    """An agent or tier named like a content field must not look like a leaked field."""
    payload = client.get("/api/dashboard/groupings?scope=recent&recent=5&by=agent").json()
    assert isinstance(payload["groups"], list)
    assert set(payload["groups"][0]) >= {"group", "requests", "samples", "models", "finish_reasons"}
    assert all(isinstance(m, dict) and "name" in m for m in payload["groups"][0]["models"])
    assert all(isinstance(r, dict) and "name" in r for r in payload["groups"][0]["finish_reasons"])


def test_dashboard_page_wires_the_config_and_grouping_panels(client):
    page = client.get("/dashboard").text
    for needle in ("/api/dashboard/config", "/api/dashboard/groupings",
                   'id="config-body"', 'id="group-body"', 'id="group-by"', 'id="group-legacy"'):
        assert needle in page, needle
    assert "read-only" in page
    assert "never shows a credential value" in page


def test_config_and_grouping_endpoints_never_expose_prompt_content(client):
    for path in ("/api/dashboard/config?scope=recent&recent=5",
                 "/api/dashboard/groupings?scope=recent&recent=5&by=agent",
                 "/api/dashboard/groupings?scope=recent&recent=5&by=band"):
        body = client.get(path)
        assert body.status_code == 200, path
        assert SENTINEL not in body.text, path
        assert not BANNED_KEYS & set(walk_keys(body.json())), path
