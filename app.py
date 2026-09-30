from __future__ import annotations
import asyncio, json, os, re, sqlite3, time, uuid
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from dashboard import router as dashboard_router

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "router_config.json"
DB_PATH = ROOT / "router_telemetry.db"

def calculate_remaining_budget_ms(deadline: float | None, now: float | None = None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - (time.perf_counter() if now is None else now) ) * 1000

def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            request_id TEXT NOT NULL, selected_tier TEXT, actual_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL NOT NULL,
            estimated_cost REAL, http_status INTEGER NOT NULL, success INTEGER NOT NULL,
            fallback_count INTEGER NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(telemetry)")}
        for name in ("agent", "session_id", "project", "task_type", "request_has_stream", "upstream_content_type", "exception_class", "upstream_http_status", "request_has_tools", "request_has_tool_choice", "total_budget_ms", "budget_exhausted", "remaining_budget_ms", "attempted_models", "attempt_outcomes", "finish_reason", "requested_tier", "routing_reason", "routing_automatic"):
            if name not in columns:
                db.execute(f"ALTER TABLE telemetry ADD COLUMN {name} {'INTEGER' if name.startswith('request_has_') or name in ('budget_exhausted', 'routing_automatic') else 'REAL' if name.endswith('_ms') else 'TEXT'}")
        db.commit()

def record_telemetry(row: dict[str, Any]) -> None:
    row.setdefault("total_budget_ms", None); row.setdefault("budget_exhausted", None); row.setdefault("remaining_budget_ms", None)
    row.setdefault("attempted_models", None); row.setdefault("attempt_outcomes", None); row.setdefault("finish_reason", None)
    row.setdefault("requested_tier", None); row.setdefault("routing_reason", None); row.setdefault("routing_automatic", None)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""INSERT INTO telemetry
            (timestamp, request_id, selected_tier, actual_model, input_tokens,
            output_tokens, latency_ms, estimated_cost, http_status, success, fallback_count,
            agent, session_id, project, task_type, request_has_stream, upstream_content_type,
            exception_class, upstream_http_status, request_has_tools, request_has_tool_choice, total_budget_ms, budget_exhausted, remaining_budget_ms, attempted_models, attempt_outcomes, finish_reason, requested_tier, routing_reason, routing_automatic)
            VALUES (:timestamp, :request_id, :selected_tier, :actual_model, :input_tokens,
                    :output_tokens, :latency_ms, :estimated_cost, :http_status, :success,
                    :fallback_count, :agent, :session_id, :project, :task_type, :request_has_stream,
                    :upstream_content_type, :exception_class, :upstream_http_status, :request_has_tools,
                    :request_has_tool_choice, :total_budget_ms, :budget_exhausted, :remaining_budget_ms, :attempted_models, :attempt_outcomes, :finish_reason, :requested_tier, :routing_reason, :routing_automatic)""", row)
        db.commit()

def api_key() -> str | None:
    name = load_config()["openrouter"].get("api_key_env", "OPENROUTER_KEY")
    return os.environ.get(name) or os.environ.get("OPENROUTER_API_KEY")

def _user_request_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str))
    return " ".join(parts).lower()

def classify_automatic_tier(body: dict[str, Any]) -> tuple[str, str]:
    """Deterministic, content-free-at-telemetry classification of user request text."""
    text = _user_request_text(body)
    if re.search(r"\b(deep|thorough|comprehensive|in[- ]depth|exhaustive)\b", text):
        return "balanced", "explicit_comprehensive_analysis"
    if re.search(r"\b(security[- ]sensitive|security review|security design|ssrf|threat model|vulnerability review|authorization review|secure design|signed webhook)\b", text):
        return "balanced", "security_sensitive"
    if re.search(r"\b(migration|migrate|migrating|cutover|deprecation plan|monolith.to.services)\b", text):
        return "balanced", "migration_planning"
    financial = re.search(r"\b(financial|payment|payments|refund|ledger|charge)\b", text)
    integrity_risk = re.search(r"\b(integrity|risk|reconciliation|workflow|data model|idempotency|partial commit|failure.mode)\b", text)
    if re.search(r"\b(financial integrity|data integrity|payment workflow|payment integrity|refund reconciliation|ledger reconciliation|financial risk)\b", text) or (financial and integrity_risk):
        return "balanced", "financial_or_data_integrity"
    architecture = re.search(r"\b(architecture|architect|system design|design a system|platform design)\b", text)
    multi_system = re.search(r"\b(multi[- ]system|distributed|multi[- ]tenant|services|service boundaries|regional failover|event[- ]driven|database and queue|multiple providers)\b", text)
    if architecture and multi_system:
        return "balanced", "multi_system_architecture"
    constraint_signals = re.findall(r"\b(tenant isolation|regional failover|auditability|zero[- ]downtime|at[- ]least[- ]once|gdpr|seven[- ]year|retention|encryption keys|bounded storage|sub[- ]minute|rollback boundary|idempotency)\b", text)
    design_task = re.search(r"\b(design|plan|propose|architect|strategy|operating strategy)\b", text)
    if (design_task and len(set(constraint_signals)) >= 2) or re.search(r"\b(complex multi[- ]constraint|conflicting requirements|multiple hard constraints)\b", text):
        return "balanced", "complex_multi_constraint_design"
    system_domains = ("gateway", "queue", "provider", "database", "inventory", "email", "telemetry", "fallback", "deadline", "audit", "regional", "service")
    if design_task and sum(bool(re.search(rf"\b{re.escape(domain)}s?\b", text)) for domain in system_domains) >= 3:
        return "balanced", "complex_multi_system_design"
    if re.search(r"\b(substantial trade.?off|trade.?off analysis|compare (?:the )?(?:\w+ )?designs|evaluate (?:the )?(?:trade.?offs|alternatives)|failure.mode analysis)\b", text):
        return "balanced", "substantial_tradeoff_analysis"
    return "fast", "default_interactive"

def select_tier(body: dict[str, Any]) -> tuple[Any, Any, str, bool, str | None]:
    """Resolve the requested model to a tier. An optional "#<tag>" suffix selects the
    same tier but is reported as the calling agent, so per-caller cost is attributable
    without inspecting request content."""
    requested_tier = body.get("model")
    if requested_tier is None or requested_tier == "auto":
        selected_tier, reason = classify_automatic_tier(body)
        return requested_tier, selected_tier, reason, True, None
    base, sep, tag = str(requested_tier).partition("#")
    # Tolerate OpenAI-style provider prefixes ("slug/balanced", "a/b/balanced"): clients
    # such as OpenHuman re-send the provider slug they stored, which is not a tier name.
    prefix = base.split("/", 1)[0] if "/" in base else None
    if prefix:
        base = base.rsplit("/", 1)[-1]
    # An explicit "#tag" wins; otherwise the provider prefix attributes the caller
    # (OpenHuman sends no tag, so this is the only way its cost is separable).
    attribution = (tag.strip() or None) if sep else prefix
    return base, base, "explicit_tier", False, attribution

app = FastAPI(title="openclaw-router", version="0.1.0")
init_db()
app.include_router(dashboard_router)

@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        with sqlite3.connect(DB_PATH) as db: db.execute("SELECT 1")
        db_ok = True
    except sqlite3.Error: db_ok = False
    return {"router_running": True, "openrouter_configured": bool(api_key()), "database_available": db_ok}

@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    created = int(time.time())
    return {"object": "list", "data": [
        {"id": tier, "object": "model", "created": created, "owned_by": "openclaw-router"}
        for tier in load_config()["tiers"]
    ]}

@app.get("/telemetry/summary")
async def telemetry_summary() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as db:
        total, successful, failed, avg_latency, input_tokens, output_tokens, cost, fallbacks = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(success), 0), COALESCE(SUM(success = 0), 0), "
            "COALESCE(AVG(latency_ms), 0), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(estimated_cost), 0), "
            "COALESCE(SUM(fallback_count), 0) FROM telemetry"
        ).fetchone()
        by_tier = {row[0]: row[1] for row in db.execute("SELECT selected_tier, COUNT(*) FROM telemetry GROUP BY selected_tier")}
        by_model = {row[0]: row[1] for row in db.execute("SELECT actual_model, COUNT(*) FROM telemetry GROUP BY actual_model")}
    return {"total_requests": total, "successful_requests": successful, "failed_requests": failed,
            "requests_by_tier": by_tier, "requests_by_actual_model": by_model,
            "average_latency_ms": round(avg_latency, 2), "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens, "total_estimated_cost": cost,
            "fallback_count": fallbacks}

def retryable_status(status: int) -> bool:
    return status not in (400, 401, 403, 422)

def error_response(upstream: httpx.Response, status: int) -> JSONResponse:
    try:
        payload = upstream.json()
    except (AttributeError, ValueError, json.JSONDecodeError):
        payload = {"error": {"message": "upstream request failed", "type": "upstream_error"}}
    return JSONResponse(payload, status_code=status)

async def close_client(client: Any) -> None:
    if hasattr(client, "aclose"):
        await client.aclose()
    else:
        await client.__aexit__(None, None, None)

def sse_lines(buffer: bytearray, chunk: bytes, final: bool = False) -> list[str]:
    """Return complete SSE lines from buffer+chunk, keeping an unterminated tail buffered.

    Upstream frames are not newline-aligned, so one `data:` event can straddle two network
    chunks; decoding each chunk in isolation loses that event's usage payload.
    """
    buffer.extend(chunk)
    lines = []
    while (cut := buffer.find(b"\n")) >= 0:
        line = bytes(buffer[:cut]).decode("utf-8", errors="ignore").rstrip("\r")
        del buffer[:cut + 1]
        lines.append(line)
    if final and buffer:
        lines.append(bytes(buffer).decode("utf-8", errors="ignore").rstrip("\r"))
        buffer.clear()
    return lines

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    started = time.perf_counter(); request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    tier = actual_model = requested_tier = routing_reason = routing_automatic = None; status = 500; success = False
    input_tokens = output_tokens = estimated_cost = None; fallback_count = 0
    request_has_stream = request_has_tools = request_has_tool_choice = None
    upstream_content_type = exception_class = upstream_http_status = finish_reason = None
    total_budget_ms = budget_exhausted = remaining_budget_ms = None
    budget_deadline = None
    attempted_models = []; attempt_outcomes = []
    def budget_remaining() -> float | None:
        value = calculate_remaining_budget_ms(budget_deadline)
        return None if value is None else value / 1000
    def note_attempt(model: str, outcome: str, status_code: int | None = None, exception: str | None = None) -> None:
        """Record what each upstream attempt actually returned.

        `upstream_http_status` holds one value and every attempt overwrites it, so on a
        request that falls back it reports only the successful last attempt - the primary's
        failure is erased. This list is what makes a fallback burst diagnosable.
        """
        attempt_outcomes.append({"model": model, "outcome": outcome,
                                 "http_status": status_code, "exception": exception})
    metadata = {"agent": request.headers.get("x-openclaw-agent"), "session_id": request.headers.get("x-openclaw-session"),
                "project": request.headers.get("x-openclaw-project"), "task_type": request.headers.get("x-task-type")}
    try:
        body = await request.json()
        requested_tier, tier, routing_reason, routing_automatic, agent_tag = select_tier(body)
        if agent_tag: metadata["agent"] = agent_tag
        budget_header = request.headers.get("x-router-total-budget-ms")
        attempt_budget_header = request.headers.get("x-router-benchmark-attempt-budgets-ms")
        attempt_budgets = [float(x) / 1000 for x in attempt_budget_header.split(",") if x.strip()] if attempt_budget_header else None
        if budget_header is not None:
            total_budget_ms = float(budget_header)
            if total_budget_ms <= 0: raise ValueError("total budget must be positive")
            budget_exhausted = 0
            budget_deadline = started + total_budget_ms / 1000
        request_has_stream = int(body.get("stream") is True); request_has_tools = int(bool(body.get("tools")))
        request_has_tool_choice = int("tool_choice" in body and body.get("tool_choice") is not None)
        cfg = load_config(); tier_cfg = cfg["tiers"].get(tier)
        if not tier_cfg: status = 400; return JSONResponse({"error":{"message":"model must be fast, balanced, or deep","type":"invalid_request_error"}}, status_code=status)
        if tier_cfg.get("provider") != "openrouter": status = 500; return JSONResponse({"error":{"message":"unsupported provider","type":"configuration_error"}}, status_code=status)
        key = api_key()
        if not key: status = 503; return JSONResponse({"error":{"message":"OpenRouter API key is not configured","type":"configuration_error"}}, status_code=status)
        models = [tier_cfg["primary"], *tier_cfg.get("fallbacks", [])]; upstream_body = dict(body)
        default_effort = {"fast": "none", "balanced": "minimal"}.get(tier)
        if default_effort:
            reasoning = upstream_body.get("reasoning")
            if not isinstance(reasoning, dict):
                reasoning = {}
            reasoning.setdefault("effort", default_effort)
            upstream_body["reasoning"] = reasoning
        headers = {"Authorization":f"Bearer {key}","Content-Type":"application/json","HTTP-Referer":"http://127.0.0.1:6060","X-Title":"openclaw-router"}
        url = cfg["openrouter"].get("base_url","https://openrouter.ai/api/v1").rstrip("/")+"/chat/completions"
        timeout = cfg["openrouter"].get("timeout_seconds", 120)
        stream_client = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            if not request_has_stream:
                for index, model in enumerate(models):
                    actual_model = model; attempted_models.append(model); upstream_body["model"] = model
                    try:
                        remaining = budget_remaining()
                        if remaining is not None and remaining <= 0:
                            budget_exhausted = 1; remaining_budget_ms = 0; status = 504
                            return JSONResponse({"error":{"message":"total request budget exhausted","type":"timeout_error"}}, status_code=status)
                        if remaining is None:
                            upstream = await client.post(url, headers=headers, json=upstream_body)
                        else:
                            attempt_timeout = min(remaining, attempt_budgets[index] if attempt_budgets and index < len(attempt_budgets) else remaining)
                            async with asyncio.timeout(attempt_timeout):
                                upstream = await client.post(url, headers=headers, json=upstream_body)
                    except TimeoutError:
                        exception_class = "TimeoutError"; note_attempt(model, "timeout", None, "TimeoutError")
                        remaining_after_timeout = budget_remaining()
                        if remaining_after_timeout and index < len(models) - 1 and (not attempt_budgets or index + 1 >= len(attempt_budgets) or attempt_budgets[index + 1] <= remaining_after_timeout):
                            fallback_count += 1; continue
                        budget_exhausted = 1 if not remaining_after_timeout else 0; remaining_budget_ms = max(0.0, (remaining_after_timeout or 0) * 1000); status = 504
                        return JSONResponse({"error":{"message":"total request budget exhausted" if budget_exhausted else "no viable fallback attempt remains","type":"timeout_error"},"attempted_models":attempted_models,"fallback_count":fallback_count,"budget_exhausted":bool(budget_exhausted),"remaining_budget_ms":remaining_budget_ms}, status_code=status)
                    except httpx.HTTPError as exc:
                        exception_class = exc.__class__.__name__; status = 502; note_attempt(model, "error", None, exception_class)
                        if index < len(models)-1: fallback_count += 1; continue
                        return JSONResponse({"error":{"message":"upstream request failed","type":"upstream_error"}}, status_code=502)
                    status = upstream.status_code; upstream_http_status = status; upstream_content_type = getattr(upstream,"headers",{}).get("content-type")
                    if 200 <= status < 300:
                        note_attempt(model, "served", status)
                        response = upstream.json(); usage = response.get("usage") or {}; input_tokens=usage.get("prompt_tokens"); output_tokens=usage.get("completion_tokens"); estimated_cost=usage.get("cost"); finish_reason=((response.get("choices") or [{}])[0] or {}).get("finish_reason"); success=True
                        response["attempted_models"] = attempted_models
                        response["fallback_count"] = fallback_count
                        response["budget_exhausted"] = bool(budget_exhausted)
                        response["remaining_budget_ms"] = budget_remaining() * 1000 if budget_remaining() is not None else None
                        return JSONResponse(response, status_code=status)
                    note_attempt(model, "rejected", status)
                    if not retryable_status(status) or index == len(models)-1: return error_response(upstream, status)
                    fallback_count += 1
                return JSONResponse({"error":{"message":"all configured models failed","type":"upstream_error"}}, status_code=502)

            selected = None; iterator = None
            stream_client = httpx.AsyncClient(timeout=timeout)
            await stream_client.__aenter__()
            for index, model in enumerate(models):
                actual_model = model; attempted_models.append(model); upstream_body["model"] = model
                try:
                    remaining = budget_remaining()
                    if remaining is not None and remaining <= 0:
                        budget_exhausted = 1; remaining_budget_ms = 0; status = 504
                        return JSONResponse({"error":{"message":"total request budget exhausted","type":"timeout_error"}}, status_code=status)
                    cm = stream_client.stream("POST", url, headers=headers, json=upstream_body)
                    if remaining is None:
                        upstream = await cm.__aenter__()
                    else:
                        async with asyncio.timeout(remaining):
                            upstream = await cm.__aenter__()
                    status = upstream.status_code; upstream_http_status = status; upstream_content_type = upstream.headers.get("content-type")
                    if not 200 <= status < 300:
                        await upstream.aread(); await cm.__aexit__(None,None,None)
                        note_attempt(model, "rejected", status)
                        if not retryable_status(status) or index == len(models)-1: return error_response(upstream, status)
                        fallback_count += 1; continue
                    iterator = upstream.aiter_bytes()
                    if remaining is None:
                        first = await iterator.__anext__()
                    else:
                        async with asyncio.timeout(budget_remaining() or 0):
                            first = await iterator.__anext__()
                    note_attempt(model, "served", status)
                    selected = (cm, upstream, iterator, first); break
                except StopAsyncIteration:
                    note_attempt(model, "empty_stream", status)
                    if index == len(models)-1: return JSONResponse({"error":{"message":"upstream returned an empty stream","type":"upstream_error"}}, status_code=502)
                    fallback_count += 1
                except TimeoutError:
                    budget_exhausted = 1; remaining_budget_ms = 0; status = 504; exception_class = "TimeoutError"
                    note_attempt(model, "timeout", None, "TimeoutError")
                    return JSONResponse({"error":{"message":"total request budget exhausted","type":"timeout_error"}}, status_code=status)
                except httpx.HTTPError as exc:
                    exception_class = exc.__class__.__name__; status = 502
                    note_attempt(model, "error", None, exception_class)
                    if index == len(models)-1: return JSONResponse({"error":{"message":"upstream request failed","type":"upstream_error"}}, status_code=502)
                    fallback_count += 1
            if selected is None:
                await close_client(stream_client)
                return JSONResponse({"error":{"message":"all configured models failed","type":"upstream_error"}}, status_code=502)
            cm, upstream, iterator, first = selected
            async def stream_body():
                nonlocal success, input_tokens, output_tokens, estimated_cost, exception_class
                sse_buffer = bytearray()
                def consume(line: str) -> None:
                    nonlocal input_tokens, output_tokens, estimated_cost, finish_reason
                    if not (line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")):
                        return
                    try:
                        payload = json.loads(line[5:].strip())
                    except (ValueError, json.JSONDecodeError):
                        return
                    usage = payload.get("usage") or {}
                    input_tokens = usage.get("prompt_tokens", input_tokens); output_tokens = usage.get("completion_tokens", output_tokens); estimated_cost = usage.get("cost", estimated_cost)
                    for choice in payload.get("choices") or []:
                        if choice.get("finish_reason"): finish_reason = choice["finish_reason"]
                def capture_usage(chunk: bytes, final: bool = False) -> None:
                    for line in sse_lines(sse_buffer, chunk, final):
                        consume(line)
                try:
                    for chunk in (first,):
                        capture_usage(chunk)
                        yield chunk
                    success = True
                    async for chunk in iterator:
                        capture_usage(chunk)
                        yield chunk
                except httpx.HTTPError as exc:
                    exception_class = exc.__class__.__name__; success = False
                finally:
                    capture_usage(b"", final=True)
                    await cm.__aexit__(None,None,None)
                    await close_client(stream_client)
                    record_telemetry({"timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"request_id":request_id,"selected_tier":tier,"requested_tier":requested_tier,"routing_reason":routing_reason,"routing_automatic":int(routing_automatic) if routing_automatic is not None else None,"actual_model":actual_model,"input_tokens":input_tokens,"output_tokens":output_tokens,"latency_ms":round((time.perf_counter()-started)*1000,2),"estimated_cost":estimated_cost,"http_status":status,"success":int(success),"fallback_count":fallback_count,"request_has_stream":request_has_stream,"upstream_content_type":upstream_content_type,"exception_class":exception_class,"upstream_http_status":upstream_http_status,"request_has_tools":request_has_tools,"request_has_tool_choice":request_has_tool_choice,"total_budget_ms":total_budget_ms,"budget_exhausted":budget_exhausted,"remaining_budget_ms":budget_remaining()*1000 if budget_remaining() is not None else None,"attempted_models":json.dumps(attempted_models),"attempt_outcomes":json.dumps(attempt_outcomes),"finish_reason":finish_reason,**metadata})
            return StreamingResponse(stream_body(), media_type="text/event-stream", status_code=200, headers={"Cache-Control":"no-cache","Connection":"keep-alive"})
    except (httpx.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        exception_class = exc.__class__.__name__; status = 504 if isinstance(exc, TimeoutError) else 502
        if isinstance(exc, TimeoutError): budget_exhausted = 1; remaining_budget_ms = 0
        return JSONResponse({"error":{"message":f"upstream request failed: {exception_class}","type":"upstream_error"}}, status_code=status)
    finally:
        if not request_has_stream or 'selected' not in locals() or selected is None:
            if request_has_stream and stream_client is not None:
                await close_client(stream_client)
            record_telemetry({"timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"request_id":request_id,"selected_tier":tier,"requested_tier":requested_tier,"routing_reason":routing_reason,"routing_automatic":int(routing_automatic) if routing_automatic is not None else None,"actual_model":actual_model,"input_tokens":input_tokens,"output_tokens":output_tokens,"latency_ms":round((time.perf_counter()-started)*1000,2),"estimated_cost":estimated_cost,"http_status":status,"success":int(success),"fallback_count":fallback_count,"request_has_stream":request_has_stream,"upstream_content_type":upstream_content_type,"exception_class":exception_class,"upstream_http_status":upstream_http_status,"request_has_tools":request_has_tools,"request_has_tool_choice":request_has_tool_choice,"total_budget_ms":total_budget_ms,"budget_exhausted":budget_exhausted,"remaining_budget_ms":budget_remaining()*1000 if budget_remaining() is not None else None,"attempted_models":json.dumps(attempted_models),"attempt_outcomes":json.dumps(attempt_outcomes),"finish_reason":finish_reason,**metadata})
