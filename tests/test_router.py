import asyncio
import asyncio
import json
import sqlite3
from fastapi.testclient import TestClient
import app
import tier_benchmark
import telemetry_report

def write_benchmark_rows(path, rows):
    path.write_text("\n\n".join(json.dumps(row, indent=2) for row in rows), encoding="utf-8")

def test_benchmark_run_scoping_excludes_legacy_and_other_runs(monkeypatch, tmp_path, capsys):
    path = tmp_path / "results.txt"; monkeypatch.setattr(tier_benchmark, "OUTPUT", path)
    write_benchmark_rows(path, [
        {"tier": "fast", "prompt_number": 1, "success": False},
        {"benchmark_run_id": "other", "tier": "fast", "prompt_number": 2, "success": True},
        {"benchmark_run_id": tier_benchmark.RUN_ID, "tier": "fast", "prompt_number": 3, "success": True, "latency_ms": 5, "input_tokens": 1, "output_tokens": 2, "estimated_cost": 0.1, "fallback_count": 0},
    ])
    rows = tier_benchmark.existing_rows()
    scoped = {(r.get("benchmark_run_id"), r.get("tier"), r.get("prompt_number")) for r in rows}
    assert (None, "fast", 1) in scoped and ("other", "fast", 2) in scoped and (tier_benchmark.RUN_ID, "fast", 3) in scoped
    tier_benchmark.status(); output = capsys.readouterr().out
    assert "CURRENT RUN: tier-benchmark-v8-interactive-quality" in output and "FAST: 1/15 completed" in output and "legacy/no-run-id: 1 rows" in output

def test_benchmark_summary_excludes_legacy_rows():
    rows = [
        {"tier": "fast", "prompt_number": 1, "success": False, "latency_ms": 90000, "input_tokens": None, "output_tokens": None, "estimated_cost": None, "fallback_count": None},
        {"benchmark_run_id": tier_benchmark.RUN_ID, "tier": "fast", "prompt_number": 1, "success": True, "latency_ms": 10, "input_tokens": 2, "output_tokens": 3, "estimated_cost": 0.2, "fallback_count": 0},
    ]
    current = [r for r in rows if r.get("benchmark_run_id") == tier_benchmark.RUN_ID]
    assert tier_benchmark.summary(current)["requests"] == 1 and tier_benchmark.summary(current)["success_rate"] == 1.0

def test_benchmark_v6_payload_and_deadline_are_scoped():
    source = open(tier_benchmark.__file__, encoding="utf-8").read()
    assert tier_benchmark.RUN_ID == "tier-benchmark-v8-interactive-quality"
    assert tier_benchmark.CLIENT_TIMEOUT_SECONDS > 90
    assert "asyncio.timeout(90)" not in source
    assert "TOTAL_BUDGET_MS = 90000" in source
    assert '"max_tokens": 2048' in source

def test_remaining_budget_uses_milliseconds():
    assert 71100 <= app.calculate_remaining_budget_ms(90.0, 18.8) <= 71300
    assert app.calculate_remaining_budget_ms(90.0, 18.8) == 71200.0

def test_benchmark_v6_run_one_sends_cap_and_budget_headers(monkeypatch):
    class Response:
        status_code = 200
        def json(self): return {"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
    class Client:
        def __init__(self, **kwargs): pass
        async def post(self, url, json, headers):
            assert json["max_tokens"] == 2048 and json["stream"] is False
            assert headers["X-Router-Total-Budget-Ms"] == "90000"
            assert headers["X-Router-Benchmark-Attempt-Budgets-Ms"] == "60000,30000"
            return Response()
    monkeypatch.setattr(tier_benchmark.httpx, "AsyncClient", Client)
    row = asyncio.run(tier_benchmark.run_one(Client(), "fast", 1, "hi"))
    assert row["success"] and row["output_tokens"] == 3

def test_v3_rows_do_not_count_toward_v4(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tier_benchmark, "OUTPUT", tmp_path / "results.txt")
    write_benchmark_rows(tier_benchmark.OUTPUT, [{"benchmark_run_id": "tier-benchmark-v3-budgeted", "tier": "fast", "prompt_number": 1}])
    tier_benchmark.status()
    assert "FAST: 0/15 completed" in capsys.readouterr().out

def test_v5_rows_do_not_count_toward_v6_and_resume_scopes_by_run_tier_prompt(tmp_path):
    rows = [{"benchmark_run_id": "tier-benchmark-v5-quality", "tier": "fast", "prompt_number": 1}, {"benchmark_run_id": tier_benchmark.RUN_ID, "tier": "fast", "prompt_number": 1}]
    missing = tier_benchmark.missing_prompts(rows, "fast")
    assert missing[0][0] == 2 and len(missing) == 14
    assert tier_benchmark.missing_prompts(rows, "balanced")[0][0] == 1

def test_v6_prompts_are_identical_to_v5_rows():
    v5 = {r["prompt_number"]: r.get("prompt") for r in tier_benchmark.existing_rows() if r.get("benchmark_run_id") == "tier-benchmark-v5-quality" and r.get("prompt")}
    assert [v5[i] for i in range(1, 16)] == tier_benchmark.PROMPTS

def test_v6_run_one_preserves_full_response_text(monkeypatch):
    class Response:
        status_code = 200
        def json(self): return {"model": "m", "choices": [{"message": {"content": "FULL RESPONSE"}, "finish_reason": "stop"}], "usage": {}}
    class Client:
        async def post(self, *args, **kwargs): return Response()
    row = asyncio.run(tier_benchmark.run_one(Client(), "fast", 1, tier_benchmark.PROMPTS[0]))
    assert row["response_text"] == "FULL RESPONSE" and row["answer"] == "FULL RESPONSE" and row["response_text_source"] == "message.content"

def test_v6_response_text_preference_order():
    base = {"choices": [{"message": {"content": "content", "reasoning_content": "reasoning-content", "reasoning": "reasoning"}, "text": "choice-text"}], "output_text": "output"}
    assert tier_benchmark.extract_response_content(base) == ("content", "message.content")
    for field, expected in [("reasoning_content", "message.reasoning_content"), ("reasoning", "message.reasoning")]:
        body = {"choices": [{"message": {field: "value"}}], "output_text": "output"}
        assert tier_benchmark.extract_response_content(body)[1] == expected
    assert tier_benchmark.extract_response_content({"choices": [{"text": "choice-text"}], "output_text": "output"})[1] == "choice.text"
    assert tier_benchmark.extract_response_content({"output_text": "output"}) == ("output", "output_text")

def test_telemetry_row_has_no_response_content(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db()
    app.record_telemetry({"timestamp":"now", "request_id":"x", "selected_tier":"fast", "actual_model":"m", "input_tokens":None, "output_tokens":None, "latency_ms":1, "estimated_cost":None, "http_status":200, "success":1, "fallback_count":0, "agent":None, "session_id":None, "project":None, "task_type":None, "request_has_stream":0, "upstream_content_type":None, "exception_class":None, "upstream_http_status":None, "request_has_tools":0, "request_has_tool_choice":0})
    with sqlite3.connect(app.DB_PATH) as db:
        assert "response_text" not in {r[1] for r in db.execute("PRAGMA table_info(telemetry)")}

def test_health(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    r = TestClient(app.app).get("/health")
    assert r.json() == {"router_running": True, "openrouter_configured": True, "database_available": True}

def test_optional_total_budget_cancels_upstream_and_records_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    cancelled = {"value": False}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            try: await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled["value"] = True; raise
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", headers={"X-Router-Total-Budget-Ms": "10"}, json={"model": "fast", "messages": []})
    assert r.status_code == 504 and cancelled["value"]
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT total_budget_ms, budget_exhausted, remaining_budget_ms FROM telemetry").fetchone()[1] == 1

def test_telemetry_metadata_and_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class FakeResponse:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return FakeResponse()
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    r = TestClient(app.app).post("/v1/chat/completions", headers={"X-OpenClaw-Agent": "personlab", "X-OpenClaw-Session": "s1", "X-OpenClaw-Project": "p1", "X-Task-Type": "test"}, json={"model": "fast", "messages": []})
    assert r.status_code == 200
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT agent, session_id, project, task_type, finish_reason FROM telemetry").fetchone() == ("personlab", "s1", "p1", "test", "stop")
    summary = TestClient(app.app).get("/telemetry/summary").json()
    assert summary["total_requests"] == 1 and summary["total_input_tokens"] == 3 and summary["total_output_tokens"] == 4

def test_missing_finish_reason_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    assert TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "messages": []}).status_code == 200
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT finish_reason FROM telemetry").fetchone() == (None,)

def test_fallback_on_model_availability_error(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []; phase = "pre"
    class FakeResponse:
        def __init__(self, status): self.status_code = status
        def json(self): return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json): calls.append(json["model"]); return FakeResponse(404 if len(calls) == 1 else 200)
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "messages": []})
    assert r.status_code == 200 and calls == [app.load_config()["tiers"]["fast"]["primary"], app.load_config()["tiers"]["fast"]["fallbacks"][0]]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT actual_model, fallback_count FROM telemetry").fetchone() == (calls[-1], 1)

def test_unknown_tier_is_rejected_and_telemetry_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db()
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "unknown", "messages": []})
    assert r.status_code == 400
    with sqlite3.connect(app.DB_PATH) as db: row = db.execute("SELECT selected_tier, http_status, success FROM telemetry").fetchone()
    assert row == ("unknown", 400, 0)

def test_routes_configured_tiers_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []; phase = "pre"
    class FakeResponse:
        status_code = 200
        def json(self): return {"id": "x", "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json): calls.append(json["model"]); return FakeResponse()
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient); client = TestClient(app.app)
    for tier in ("fast", "balanced", "deep"):
        r = client.post("/v1/chat/completions", json={"model": tier, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and r.json()["choices"]
    assert calls == [app.load_config()["tiers"][tier]["primary"] for tier in ("fast", "balanced", "deep")]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT COUNT(*) FROM telemetry WHERE success=1").fetchone()[0] == 3

def test_streaming_forwards_sse_and_done_without_json(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class StreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self):
            yield b"data: {\\\"choices\\\":[{}]}\\n\\n"
            yield b"data: [DONE]\\n\\n"
        async def aread(self): return b""
        def json(self): raise AssertionError("streaming response must not call json()")
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): return StreamResponse()
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    assert "[DONE]" in r.text
    with sqlite3.connect(app.DB_PATH) as db:
        row = db.execute("SELECT request_has_stream, upstream_content_type, success FROM telemetry").fetchone()
    assert row == (1, "text/event-stream", 1)

def test_streaming_fallback_before_first_byte(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class StreamResponse:
        def __init__(self, status): self.status_code=status; self.headers={"content-type":"application/json"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self):
            if self.status_code == 200: yield b"data: [DONE]\\n\\n"
        async def aread(self): return b"{}"
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): calls.append(kwargs["json"]["model"]); return StreamResponse(404 if len(calls)==1 else 200)
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code == 200 and calls == [app.load_config()["tiers"]["fast"]["primary"], app.load_config()["tiers"]["fast"]["fallbacks"][0]]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count, success FROM telemetry").fetchone() == (1, 1)

def test_streaming_does_not_fallback_after_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class StreamResponse:
        status_code=200; headers={"content-type":"text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self):
            yield b"data: first\\n\\n"
            raise app.httpx.ReadError("after first byte")
        async def aread(self): return b""
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): calls.append(kwargs["json"]["model"]); return StreamResponse()
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code == 200 and calls == [app.load_config()["tiers"]["fast"]["primary"]]

def test_non_streaming_network_failure_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls=[]
    class Response:
        status_code=200; headers={"content-type":"application/json"}
        def json(self): return {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,headers,json):
            calls.append(json["model"])
            if len(calls)==1: raise app.httpx.ConnectError("down")
            return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    r=TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","messages":[]})
    assert r.status_code==200 and len(calls)==2
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count,success FROM telemetry").fetchone()==(1,1)

def test_non_streaming_network_failure_exhausts_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,*args,**kwargs): raise app.httpx.ConnectError("down")
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    r=TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","messages":[]})
    assert r.status_code==502
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count,success FROM telemetry").fetchone()==(1,0)

def test_streaming_no_fallback_returns_non_200(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    cfg=app.load_config(); cfg["tiers"]["fast"]["fallbacks"]=[]; monkeypatch.setattr(app,"load_config",lambda:cfg)
    class Response:
        status_code=503; headers={"content-type":"application/json"}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aread(self): return b"{}"
        async def aiter_bytes(self):
            if False: yield b""
        def json(self): return {"error":{"message":"unavailable"}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kwargs): return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    r=TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code==503
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT success FROM telemetry").fetchone()==(0,)

def test_streaming_usage_is_recorded_when_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class Response:
        status_code=200; headers={"content-type":"text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aiter_bytes(self): yield b'data: {"usage":{"prompt_tokens":9,"completion_tokens":4,"cost":0.12}}\n\ndata: [DONE]\n\n'
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kwargs): return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    r=TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code==200
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT input_tokens,output_tokens,estimated_cost FROM telemetry").fetchone()==(9,4,0.12)

def test_streaming_usage_absent_remains_null(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class Response:
        status_code=200; headers={"content-type":"text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aiter_bytes(self): yield b"data: [DONE]\n\n"
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kwargs): return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT input_tokens,output_tokens,estimated_cost FROM telemetry").fetchone()==(None,None,None)

USAGE_EVENT = b'data: {"id":1,"usage":{"prompt_tokens":9,"completion_tokens":4,"cost":0.12},"choices":[{"finish_reason":"stop"}]}\n\n'

def post_stream(monkeypatch, tmp_path, chunks):
    """Route a streamed response whose upstream frames are exactly `chunks`."""
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    class Response:
        status_code=200; headers={"content-type":"text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aiter_bytes(self):
            for chunk in chunks: yield chunk
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kwargs): return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    return TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})

def recorded_tokens(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT input_tokens,output_tokens,estimated_cost,finish_reason FROM telemetry").fetchone()

def test_streaming_usage_survives_a_split_mid_usage_line(monkeypatch, tmp_path):
    cut = len(USAGE_EVENT) // 2
    response = post_stream(monkeypatch, tmp_path, [USAGE_EVENT[:cut], USAGE_EVENT[cut:]])
    assert response.status_code == 200
    assert recorded_tokens(tmp_path / "telemetry.db") == (9, 4, 0.12, "stop")

def test_streaming_usage_survives_byte_by_byte_delivery(monkeypatch, tmp_path):
    stream = USAGE_EVENT + b"data: [DONE]\n\n"
    response = post_stream(monkeypatch, tmp_path, [stream[i:i + 1] for i in range(len(stream))])
    assert response.status_code == 200
    assert recorded_tokens(tmp_path / "telemetry.db") == (9, 4, 0.12, "stop")

def test_streaming_usage_survives_a_split_mid_crlf_terminator(monkeypatch, tmp_path):
    crlf = USAGE_EVENT.replace(b"\n\n", b"\r\n\r\n")
    cut = crlf.index(b"\r\n\r\n") + 1
    response = post_stream(monkeypatch, tmp_path, [crlf[:cut], crlf[cut:]])
    assert response.status_code == 200
    assert recorded_tokens(tmp_path / "telemetry.db") == (9, 4, 0.12, "stop")

def test_streaming_usage_in_an_unterminated_final_line_is_still_recorded(monkeypatch, tmp_path):
    response = post_stream(monkeypatch, tmp_path, [USAGE_EVENT.rstrip(b"\n")])
    assert response.status_code == 200
    assert recorded_tokens(tmp_path / "telemetry.db") == (9, 4, 0.12, "stop")

def test_fragmented_streaming_usage_still_forwards_upstream_bytes_verbatim(monkeypatch, tmp_path):
    stream = USAGE_EVENT + b"data: [DONE]\n\n"
    chunks = [stream[i:i + 7] for i in range(0, len(stream), 7)]
    response = post_stream(monkeypatch, tmp_path, chunks)
    assert response.content == stream
    assert recorded_tokens(tmp_path / "telemetry.db") == (9, 4, 0.12, "stop")

def test_sse_lines_buffers_only_the_unterminated_tail():
    buffer = bytearray()
    assert app.sse_lines(buffer, b"data: one\nda") == ["data: one"]
    assert bytes(buffer) == b"da"
    assert app.sse_lines(buffer, b"ta: two\r\ndata: three\r") == ["data: two"]
    assert bytes(buffer) == b"data: three\r"
    assert app.sse_lines(buffer, b"\n", final=True) == ["data: three"]
    assert bytes(buffer) == b""
    assert app.sse_lines(buffer, b"", final=True) == []


def recorded_attempt_outcomes(db_path):
    with sqlite3.connect(db_path) as db:
        raw = db.execute("SELECT attempt_outcomes FROM telemetry").fetchone()[0]
    return json.loads(raw) if raw else None


def streaming_client(monkeypatch, tmp_path, status_for_attempt, chunks_for_attempt):
    """Route streamed attempts whose i-th call returns the given status and body chunks."""
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class StreamResponse:
        def __init__(self, index):
            self.index = index; self.status_code = status_for_attempt(index)
            self.headers = {"content-type": "text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self):
            for chunk in chunks_for_attempt(self.index): yield chunk
        async def aread(self): return b"{}"
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): calls.append(kwargs["json"]["model"]); return StreamResponse(len(calls) - 1)
    monkeypatch.setattr(app.httpx, "AsyncClient", FakeClient)
    return calls

def fast_primary(): return app.load_config()["tiers"]["fast"]["primary"]
def fast_fallback(): return app.load_config()["tiers"]["fast"]["fallbacks"][0]

def test_streaming_fallback_keeps_the_primary_status_the_last_attempt_used_to_erase(monkeypatch, tmp_path):
    """`upstream_http_status` holds one value, so a fallback overwrote the primary's failure."""
    streaming_client(monkeypatch, tmp_path, lambda i: 503 if i == 0 else 200,
                     lambda i: [b"data: [DONE]\n\n"] if i else [])
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "stream": True, "messages": []})
    assert r.status_code == 200
    assert recorded_attempt_outcomes(tmp_path / "telemetry.db") == [
        {"model": fast_primary(), "outcome": "rejected", "http_status": 503, "exception": None},
        {"model": fast_fallback(), "outcome": "served", "http_status": 200, "exception": None},
    ]
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        stored = db.execute("SELECT upstream_http_status FROM telemetry").fetchone()[0]
    # The column has TEXT affinity (init_db types anything not ending _ms as TEXT); the
    # point of the assertion is which attempt's status survived, not the storage class.
    assert int(stored) == 200

def test_streaming_empty_200_is_recorded_as_its_own_outcome(monkeypatch, tmp_path):
    """A primary that connects, returns 200 and yields zero chunks is not the same fault as a 5xx."""
    streaming_client(monkeypatch, tmp_path, lambda i: 200, lambda i: [] if i == 0 else [b"data: [DONE]\n\n"])
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "stream": True, "messages": []})
    assert r.status_code == 200
    outcomes = recorded_attempt_outcomes(tmp_path / "telemetry.db")
    assert outcomes[0] == {"model": fast_primary(), "outcome": "empty_stream", "http_status": 200, "exception": None}
    assert outcomes[1]["outcome"] == "served"
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        assert db.execute("SELECT fallback_count, success FROM telemetry").fetchone() == (1, 1)

def test_streaming_upstream_exception_is_named_per_attempt(monkeypatch, tmp_path):
    def chunks(i):
        if i == 0: raise app.httpx.ConnectError("nope")
        return [b"data: [DONE]\n\n"]
    streaming_client(monkeypatch, tmp_path, lambda i: 200, chunks)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "stream": True, "messages": []})
    assert r.status_code == 200
    outcomes = recorded_attempt_outcomes(tmp_path / "telemetry.db")
    assert outcomes[0] == {"model": fast_primary(), "outcome": "error", "http_status": None, "exception": "ConnectError"}

def test_non_streaming_fallback_records_both_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        def __init__(self, index):
            self.status_code = 429 if index == 0 else 200
            self.headers = {"content-type": "application/json"}
        def json(self):
            return {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.01}}
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): calls.append(kwargs["json"]["model"]); return Response(len(calls) - 1)
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "messages": []})
    assert r.status_code == 200
    assert recorded_attempt_outcomes(tmp_path / "telemetry.db") == [
        {"model": fast_primary(), "outcome": "rejected", "http_status": 429, "exception": None},
        {"model": fast_fallback(), "outcome": "served", "http_status": 200, "exception": None},
    ]

def test_a_clean_single_attempt_records_exactly_one_outcome(monkeypatch, tmp_path):
    streaming_client(monkeypatch, tmp_path, lambda i: 200, lambda i: [b"data: [DONE]\n\n"])
    TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "stream": True, "messages": []})
    assert recorded_attempt_outcomes(tmp_path / "telemetry.db") == [
        {"model": fast_primary(), "outcome": "served", "http_status": 200, "exception": None}]

def test_malformed_request_body_records_telemetry_instead_of_throwing_from_the_finally(monkeypatch, tmp_path):
    """The outer finally reads budget_remaining(), defined after request.json() used to run."""
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    r = TestClient(app.app).post("/v1/chat/completions", content=b"{not json",
                                 headers={"content-type": "application/json"})
    assert r.status_code == 502
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        row = db.execute("SELECT http_status, success, attempt_outcomes FROM telemetry").fetchone()
    assert row == (502, 0, "[]")

def test_malformed_budget_headers_record_telemetry_instead_of_a_500(monkeypatch, tmp_path):
    """float(budget_header), the <=0 guard and the attempt-budget list all raise before the old def site."""
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    for header in ("not-a-number", "-1", "0"):
        r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "messages": []},
                                     headers={"x-router-total-budget-ms": header})
        assert r.status_code == 502, header
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "fast", "messages": []},
                                 headers={"x-router-benchmark-attempt-budgets-ms": "abc"})
    assert r.status_code == 502
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        rows = db.execute("SELECT http_status, success FROM telemetry").fetchall()
    assert len(rows) == 4 and all(row == (502, 0) for row in rows)

def test_streaming_client_and_response_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    state = {"stream_client_open": False, "response_open": False, "client_closed": False, "response_closed": False, "observed_open": False, "clients": 0, "stream_owner": None}
    class Response:
        status_code=200; headers={"content-type":"text/event-stream"}
        def __init__(self, owner): self.owner=owner
        async def __aenter__(self): state["response_open"]=True; return self
        async def __aexit__(self,*args): state["response_open"]=False; state["response_closed"]=True
        async def aiter_bytes(self):
            assert state["stream_client_open"] and state["response_open"]; state["observed_open"]=True
            yield b"data: one\n\n"; await asyncio.sleep(0.01)
            assert state["stream_client_open"] and state["response_open"]
            yield b"data: [DONE]\n\n"
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self):
            state["clients"] += 1
            if state["clients"] == 2: state["stream_client_open"] = True; state["stream_owner"] = self
            return self
        async def __aexit__(self,*args):
            if self is state["stream_owner"] and state["stream_client_open"]: state["stream_client_open"] = False; state["client_closed"] = True
        def stream(self,*args,**kwargs): return Response(self)
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    r=TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})
    assert r.status_code==200 and "data: [DONE]" in r.text
    assert state["observed_open"] and state["response_closed"] and state["client_closed"]

def test_streaming_cancellation_closes_resources(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    state = {"closed": False}
    class Response:
        status_code=200; headers={"content-type":"text/event-stream"}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): state["closed"]=True
        async def aiter_bytes(self):
            yield b"data: first\n\n"
            raise asyncio.CancelledError()
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): state["closed"]=True
        def stream(self,*args,**kwargs): return Response()
    monkeypatch.setattr(app.httpx,"AsyncClient",Client)
    TestClient(app.app).post("/v1/chat/completions",json={"model":"fast","stream":True,"messages":[]})
    assert state["closed"]


def test_deep_primary_read_timeout_falls_back_to_qwen(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        status_code = 200; headers = {"content-type": "application/json"}
        def json(self): return {"model": "qwen/qwen3.8-max-0902", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append(json["model"])
            if len(calls) == 1: raise app.httpx.ReadTimeout("primary timeout")
            return Response()
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "deep", "messages": []})
    assert r.status_code == 200 and calls == ["minimax/minimax-m3", "qwen/qwen3.8-max-0902"]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT actual_model, fallback_count FROM telemetry").fetchone() == ("qwen/qwen3.8-max-0902", 1)


def test_deep_primary_timeout_and_qwen_503_falls_back_to_deepseek(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        def __init__(self, status): self.status_code = status; self.headers = {"content-type": "application/json"}
        def json(self): return {"model": "deepseek/deepseek-v4-flash", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json):
            calls.append(json["model"])
            if len(calls) == 1: raise app.httpx.ReadTimeout("primary timeout")
            return Response(503 if len(calls) == 2 else 200)
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "deep", "messages": []})
    assert r.status_code == 200 and calls == ["minimax/minimax-m3", "qwen/qwen3.8-max-0902", "deepseek/deepseek-v4-flash"]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT actual_model, fallback_count FROM telemetry").fetchone() == ("deepseek/deepseek-v4-flash", 2)


def test_deep_400_does_not_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        status_code = 400; headers = {"content-type": "application/json"}
        def json(self): return {"error": {"message": "bad request"}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json): calls.append(json["model"]); return Response()
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "deep", "messages": []})
    assert r.status_code == 400 and calls == ["minimax/minimax-m3"]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count FROM telemetry").fetchone() == (0,)


def test_deep_streaming_pre_byte_fallback_and_post_byte_protection(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []; phase = "pre"
    class Response:
        status_code = 200; headers = {"content-type": "text/event-stream"}
        def __init__(self, fail=False): self.fail = fail
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_bytes(self):
            if self.fail: raise app.httpx.ReadTimeout("before first byte")
            yield b"data: first\n\n"; raise app.httpx.ReadError("after first byte")
        async def aread(self): return b""
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            calls.append(kwargs["json"]["model"])
            return Response(fail=phase == "pre" and len(calls) == 1)
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "deep", "stream": True, "messages": []})
    assert r.status_code == 200 and calls == ["minimax/minimax-m3", "qwen/qwen3.8-max-0902"]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count FROM telemetry").fetchone() == (1,)

    calls.clear(); phase = "post"; monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry2.db"); app.init_db()
    r = TestClient(app.app).post("/v1/chat/completions", json={"model": "deep", "stream": True, "messages": []})
    assert r.status_code == 200 and calls == ["minimax/minimax-m3"]
    with sqlite3.connect(app.DB_PATH) as db: assert db.execute("SELECT fallback_count FROM telemetry").fetchone() == (0,)


def _user_body(text, model=None):
    body = {"messages": [{"role": "user", "content": text}]}
    if model is not None:
        body["model"] = model
    return body


def test_automatic_tier_routing_default_cases_are_fast():
    routine = [
        "What is a Python list comprehension?",
        "Fix this normal coding bug in my loop.",
        "Summarize this short note.",
        "Give a straightforward analysis of this bounded question.",
        "I need a latency-sensitive answer.",
    ]
    for text in routine:
        assert app.select_tier(_user_body(text)) == (None, "fast", "default_interactive", True, None)


def test_automatic_tier_routing_covers_policy_triggers():
    cases = [
        ("Design a production architecture for a distributed multi-tenant event-driven platform with regional failover.", "balanced", "multi_system_architecture"),
        ("Review this security-sensitive webhook design for SSRF and authorization flaws.", "balanced", "security_sensitive"),
        ("Plan a migration from a monolith to services with rollback boundaries.", "balanced", "migration_planning"),
        ("Design a system with immutable inputs, GDPR deletion, seven-year retention, encryption keys, and bounded storage.", "balanced", "complex_multi_constraint_design"),
        ("Synthesize a technical strategy for operating a model gateway across APIs, providers, fallbacks, telemetry, and deadlines.", "balanced", "complex_multi_system_design"),
        ("Review payment workflow idempotency and financial data integrity risks.", "balanced", "financial_or_data_integrity"),
        ("Compare three designs and analyze substantial tradeoffs.", "balanced", "substantial_tradeoff_analysis"),
        ("Give me a comprehensive analysis of this bounded problem.", "balanced", "explicit_comprehensive_analysis"),
    ]
    for text, tier, reason in cases:
        assert app.select_tier(_user_body(text)) == (None, tier, reason, True, None)


def test_integrity_audit_flags_blind_spots_and_groups_hours(monkeypatch, tmp_path):
    """Regression guard: the hour buckets must be grouped, not a whole-table aggregate,
    and the scope must apply to every counter including the buckets."""
    db_file = tmp_path / "tel.db"
    monkeypatch.setattr(app, "DB_PATH", db_file)
    app.init_db()
    rows = [
        # (timestamp, input_tokens, estimated_cost, success, http_status, agent)
        ("2026-01-01T10:00:00Z", None, None, 1, 200, "utility"),   # blind, hour 10
        ("2026-01-01T11:00:00Z", None, None, 1, 200, "utility"),   # blind, hour 11
        ("2026-01-01T11:30:00Z", None, None, 1, 200, "utility"),   # blind, hour 11
        ("2026-01-01T12:00:00Z", 100, 0.01, 1, 200, "writer"),     # measured
        ("2026-01-01T12:10:00Z", 200, 0.02, 1, 200, "writer"),     # measured
        ("2026-01-01T12:20:00Z", 50, 0.001, 0, 504, "writer"),     # genuine failure
    ]
    with sqlite3.connect(db_file) as con:
        con.executemany(
            "INSERT INTO telemetry (timestamp, selected_tier, actual_model, input_tokens,"
            " estimated_cost, success, http_status, latency_ms, request_id, agent,"
            " request_has_stream, attempted_models, routing_reason)"
            " VALUES (?, 'fast', 'm', ?, ?, ?, ?, 10, ?, ?, 1, '[]', 'explicit_tier')",
            [(ts, it, ec, s, hs, f"r{n}", ag) for n, (ts, it, ec, s, hs, ag) in enumerate(rows)],
        )
        # A legacy blind row from another code path must not count as a live finding.
        con.execute(
            "INSERT INTO telemetry (timestamp, selected_tier, actual_model, input_tokens,"
            " estimated_cost, success, http_status, latency_ms, request_id, request_has_stream,"
            " attempted_models, routing_reason) VALUES"
            " ('2026-01-01T13:00:00Z', 'fast', 'm', NULL, NULL, 1, 200, 10, 'legacy1', 1, '[]', NULL)")
    monkeypatch.setattr(telemetry_report, "DB_PATH", db_file)

    full = telemetry_report.check_integrity()
    assert full["verdict"] == "UNRELIABLE"
    assert full["scope_rows"] == 7
    assert full["blind_live"] == 3 and full["blind_legacy"] == 1, full
    assert any("3/7 current-path rows" in f for f in full["findings"]), full["findings"]
    assert any("1 blind rows are legacy" in w for w in full["warnings"]), full["warnings"]
    # The key regression: buckets must be per-hour (11 -> 2, 10 -> 1), never one merged row,
    # and must exclude the legacy row.
    buckets = {c["hour"]: c["rows"] for c in full["blind_spot_clusters"]}
    assert buckets == {"2026-01-01T11": 2, "2026-01-01T10": 1}, buckets
    assert sum(buckets.values()) == 3

    # Scoping must shrink the blind count AND the buckets consistently. Scope is by id, so
    # recent=5 keeps ids 3-7 (one live blind + the legacy row) and recent=4 keeps ids 4-7
    # (no live blind left), which must read as OK rather than inheriting the legacy row.
    scoped5 = telemetry_report.check_integrity(recent=5)
    assert scoped5["scope_rows"] == 5
    assert scoped5["blind_live"] == 1 and scoped5["blind_legacy"] == 1, scoped5
    assert any("1/5 current-path rows" in f for f in scoped5["findings"]), scoped5["findings"]
    assert sum(c["rows"] for c in scoped5["blind_spot_clusters"]) == 1

    scoped4 = telemetry_report.check_integrity(recent=4)
    assert scoped4["scope_rows"] == 4
    assert scoped4["blind_live"] == 0, scoped4
    assert scoped4["verdict"] == "OK", scoped4["findings"]

    # A clean table reports OK rather than inventing findings.
    with sqlite3.connect(db_file) as con:
        con.execute("DELETE FROM telemetry WHERE input_tokens IS NULL")
    assert telemetry_report.check_integrity()["verdict"] == "OK"


def test_explicit_tier_overrides_automatic_policy():
    assert app.select_tier(_user_body("Review this security-sensitive architecture.", "fast")) == ("fast", "fast", "explicit_tier", False, None)
    assert app.select_tier(_user_body("What is two plus two?", "balanced")) == ("balanced", "balanced", "explicit_tier", False, None)
    assert app.select_tier(_user_body("ordinary request", "deep")) == ("deep", "deep", "explicit_tier", False, None)
    assert app.select_tier(_user_body("ordinary request", "auto")) == ("auto", "fast", "default_interactive", True, None)


def test_agent_suffix_selects_same_tier_and_reports_caller():
    """`#<tag>` attributes cost per caller without changing routing or inspecting content."""
    assert app.select_tier(_user_body("anything", "balanced#coordinator")) == (
        "balanced", "balanced", "explicit_tier", False, "coordinator")
    assert app.select_tier(_user_body("anything", "fast#utility")) == (
        "fast", "fast", "explicit_tier", False, "utility")
    # The suffix must not leak into the tier used for config lookup or telemetry.
    tier, selected, reason, automatic, tag = app.select_tier(_user_body("x", "deep#reviewer"))
    assert selected == "deep" and tag == "reviewer"
    # An empty suffix is treated as no attribution, not an empty-string agent.
    assert app.select_tier(_user_body("x", "balanced#")) == ("balanced", "balanced", "explicit_tier", False, None)
    assert app.select_tier(_user_body("x", "balanced#  ")) == ("balanced", "balanced", "explicit_tier", False, None)
    # Auto routing is unaffected by the suffix path.
    assert app.select_tier(_user_body("x", "auto"))[4] is None


def test_provider_prefixed_model_ids_resolve_to_their_tier():
    """Clients that re-send a stored provider slug (OpenHuman, OpenClaw) must not 400.
    The first element is the NORMALIZED requested tier, matching how '#tag' is stripped."""
    assert app.select_tier(_user_body("x", "openclaw-router/balanced")) == (
        "balanced", "balanced", "explicit_tier", False, "openclaw-router")
    assert app.select_tier(_user_body("x", "some-slug/fast"))[1] == "fast"
    assert app.select_tier(_user_body("x", "some-slug/fast"))[4] == "some-slug"
    assert app.select_tier(_user_body("x", "a/b/deep"))[1] == "deep"
    # An untagged provider-prefixed id attributes to the prefix, so OpenHuman-style
    # clients are separable in telemetry without sending a '#tag'.
    assert app.select_tier(_user_body("x", "router-local/balanced"))[4] == "router-local"
    # Prefix and attribution suffix compose.
    assert app.select_tier(_user_body("x", "openhuman/balanced#openhuman")) == (
        "balanced", "balanced", "explicit_tier", False, "openhuman")
    # OpenClaw's existing provider-prefixed, tagged form is unchanged.
    assert app.select_tier(_user_body("x", "router-local/balanced#xxpress"))[1] == "balanced"
    assert app.select_tier(_user_body("x", "router-local/balanced#xxpress"))[4] == "xxpress"


def test_automatic_route_applies_tier_reasoning_and_private_decision_telemetry(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json): calls.append(json); return Response()
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    prompt = "Design a distributed multi-system architecture with regional failover. PRIVATE_PROMPT_MARKER"
    response = TestClient(app.app).post("/v1/chat/completions", json=_user_body(prompt))
    assert response.status_code == 200
    assert calls[0]["model"] == app.load_config()["tiers"]["balanced"]["primary"]
    assert calls[0]["reasoning"]["effort"] == "minimal"
    with sqlite3.connect(app.DB_PATH) as db:
        row = db.execute("SELECT requested_tier, selected_tier, routing_reason, routing_automatic FROM telemetry").fetchone()
        stored = " ".join(str(value) for value in db.execute("SELECT * FROM telemetry").fetchone())
    assert row == (None, "balanced", "multi_system_architecture", 1)
    assert "PRIVATE_PROMPT_MARKER" not in stored


def test_automatic_fast_reasoning_and_explicit_reasoning_override(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "telemetry.db"); app.init_db(); monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    calls = []
    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, headers, json): calls.append(json); return Response()
    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    client = TestClient(app.app)
    assert client.post("/v1/chat/completions", json=_user_body("Summarize this note.")).status_code == 200
    assert calls[-1]["model"] == app.load_config()["tiers"]["fast"]["primary"]
    assert calls[-1]["reasoning"]["effort"] == "none"
    assert client.post("/v1/chat/completions", json={**_user_body("Review a security design.", "fast"), "reasoning": {"effort": "high"}}).status_code == 200
    assert calls[-1]["model"] == app.load_config()["tiers"]["fast"]["primary"]
    assert calls[-1]["reasoning"]["effort"] == "high"


def test_recent_routing_summary_calculations():
    rows = [
        {"selected_tier":"fast", "success":1, "latency_ms":10, "estimated_cost":0.1, "input_tokens":10, "output_tokens":5, "fallback_count":0, "budget_exhausted":0, "finish_reason":"stop", "routing_automatic":1, "routing_reason":"default_interactive", "requested_tier":None, "actual_model":"fast-a", "attempted_models":"[\"fast-a\"]"},
        {"selected_tier":"fast", "success":0, "latency_ms":20, "estimated_cost":0.3, "input_tokens":20, "output_tokens":8, "fallback_count":1, "budget_exhausted":1, "finish_reason":"length", "routing_automatic":0, "routing_reason":"explicit_tier", "requested_tier":"balanced", "actual_model":"fast-b", "attempted_models":"[\"fast-a\",\"fast-b\"]"},
        {"selected_tier":"balanced", "success":1, "latency_ms":30, "estimated_cost":0.9, "input_tokens":30, "output_tokens":15, "fallback_count":0, "budget_exhausted":0, "finish_reason":"stop", "routing_automatic":1, "routing_reason":"migration_planning", "requested_tier":None, "actual_model":"balanced-a", "attempted_models":"[\"balanced-a\"]"},
        {"selected_tier":"balanced", "success":1, "latency_ms":50, "estimated_cost":None, "input_tokens":40, "output_tokens":20, "fallback_count":0, "budget_exhausted":0, "finish_reason":None, "routing_automatic":0, "routing_reason":"explicit_tier", "requested_tier":"balanced", "actual_model":"balanced-a", "attempted_models":"[\"balanced-a\"]"},
    ]
    report = telemetry_report.summarize_routing_rows(rows)
    assert report["total_requests"] == 4 and report["success_rate_percent"] == 75.0
    assert report["routing_tier_counts"]["fast"] == {"count":2,"percent":50.0}
    assert report["automatic_vs_explicit"] == {"automatic":2,"explicit":2,"unknown_or_legacy":0}
    assert report["requested_tier_mismatches"]["count"] == 1
    fast = report["tier_operations"]["fast"]
    assert fast["average_latency_ms"] == 15 and fast["median_latency_ms"] == 15 and fast["p95_latency_ms"] == 20
    assert fast["total_cost"] == 0.4 and fast["average_cost_per_request"] == 0.2
    assert fast["input_tokens"] == 30 and fast["output_tokens"] == 13
    assert fast["fallback_rate_percent"] == 50 and fast["budget_exhaustion_rate_percent"] == 50 and fast["error_rate_percent"] == 50
    assert fast["finish_reason_distribution"] == {"stop":1,"length":1}
    assert report["attempted_model_sequences"]["fast-a -> fast-b"] == 1
    assert report["balanced_reason_diagnostics"]["dominant_reason"] == "migration_planning"
    assert report["balanced_reason_diagnostics"]["concentration_flag"] is False


def test_routing_report_uses_recent_window_and_never_prints_content(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(telemetry_report, "DB_PATH", tmp_path / "telemetry.db")
    monkeypatch.setattr(app, "DB_PATH", telemetry_report.DB_PATH)
    app.init_db()
    app.record_telemetry({"timestamp":"now", "request_id":"r1", "selected_tier":"fast", "actual_model":"fast-model", "input_tokens":1, "output_tokens":1, "latency_ms":1, "estimated_cost":0, "http_status":200, "success":1, "fallback_count":0, "agent":None, "session_id":None, "project":None, "task_type":None, "request_has_stream":0, "upstream_content_type":None, "exception_class":None, "upstream_http_status":None, "request_has_tools":0, "request_has_tool_choice":0, "routing_reason":"default_interactive", "routing_automatic":1, "requested_tier":None, "attempted_models":"[\"fast-model\"]"})
    result = telemetry_report.routing_report(1)
    assert result["total_requests"] == 1 and result["window"]["returned"] == 1
    telemetry_report.print_routing_report(1)
    output = capsys.readouterr().out
    assert "prompt" not in output.lower() and "response" not in output.lower()
