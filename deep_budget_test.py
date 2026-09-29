import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import benchmark_deep

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "router_config.json").read_text(encoding="utf-8"))
CHAIN = [CFG["tiers"]["deep"]["primary"], *CFG["tiers"]["deep"]["fallbacks"]]
URL = CFG["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
KEY_NAME = CFG["openrouter"].get("api_key_env", "OPENROUTER_KEY")
KEY = os.environ.get(KEY_NAME) or os.environ.get("OPENROUTER_API_KEY")
OUTPUT = ROOT / "deep_budget_test.txt"
OVERALL = 90.0
LIMITS = [60.0, 30.0, 30.0]


async def attempt(client, model, prompt, number, attempt_number, timeout, request_started, budget_before):
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    record = {"model": model, "attempt_number": attempt_number, "attempt_start_time": started_at, "budget_before_seconds": round(budget_before, 3), "hard_timeout": False, "http_status": None, "success": False, "tokens": {"input": None, "output": None}, "estimated_cost": None, "exception": None}
    try:
        async with asyncio.timeout(timeout):
            response = await client.post(URL, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False})
            record["http_status"] = response.status_code
            if 200 <= response.status_code < 300:
                usage = (response.json().get("usage") or {})
                record["success"] = True; record["tokens"] = {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}; record["estimated_cost"] = usage.get("cost")
            elif response.status_code in (400, 401, 403):
                record["exception"] = "non_retryable_http_error"
            else:
                record["exception"] = "retryable_http_error"
    except TimeoutError:
        record["hard_timeout"] = True; record["exception"] = "TimeoutError"
    except Exception as exc:
        record["exception"] = type(exc).__name__
    record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    record["budget_after_seconds"] = round(max(0.0, OVERALL - (time.perf_counter() - request_started)), 3)
    return record


async def run():
    if not KEY: raise SystemExit("OpenRouter credential is not configured")
    records = []
    async with httpx.AsyncClient(timeout=None) as client:
        with OUTPUT.open("w", encoding="utf-8") as out:
            out.write(f"chain={json.dumps(CHAIN)}\noverall_budget_seconds={OVERALL}\nattempt_limits_seconds={LIMITS}\n\n")
            for number, prompt in enumerate(benchmark_deep.PROMPTS, 1):
                request_started = time.perf_counter(); attempts = []; overall_expired = False
                for index, model in enumerate(CHAIN):
                    remaining = OVERALL - (time.perf_counter() - request_started)
                    if remaining <= 0:
                        overall_expired = True; break
                    result = await attempt(client, model, prompt, number, index + 1, min(LIMITS[index], remaining), request_started, remaining)
                    attempts.append(result)
                    if result["success"] or result["exception"] == "non_retryable_http_error": break
                if not attempts or (not attempts[-1]["success"] and OVERALL - (time.perf_counter() - request_started) <= 0): overall_expired = True
                final = attempts[-1] if attempts else None
                record = {"request_number": number, "attempts": attempts, "fallback_count": max(0, len(attempts) - 1), "total_request_latency_ms": round((time.perf_counter() - request_started) * 1000, 2), "overall_budget_expired": overall_expired, "final_model": final["model"] if final and final["success"] else None, "final_success": bool(final and final["success"]), "final_result": final["exception"] if final and not final["success"] else "success", "total_estimated_cost": sum(a["estimated_cost"] or 0 for a in attempts)}
                records.append(record); out.write(json.dumps(record, indent=2) + "\n\n"); out.flush(); print(json.dumps(record, ensure_ascii=True))
            latencies = [r["total_request_latency_ms"] for r in records]
            summary = {"requests_attempted": len(records), "successes": sum(r["final_success"] for r in records), "failures": sum(not r["final_success"] for r in records), "minimax_completions": sum(r["attempts"] and r["attempts"][0]["success"] for r in records), "minimax_timeouts": sum(r["attempts"] and r["attempts"][0]["hard_timeout"] for r in records), "qwen_recoveries": sum(r["final_model"] == CHAIN[1] for r in records), "deepseek_recoveries": sum(r["final_model"] == CHAIN[2] for r in records), "overall_budget_expired": sum(r["overall_budget_expired"] for r in records), "average_latency_ms": round(sum(latencies) / len(latencies), 2), "median_latency_ms": round(statistics.median(latencies), 2), "maximum_latency_ms": max(latencies), "p95_latency_ms": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 2), "total_estimated_cost": sum(r["total_estimated_cost"] for r in records), "fallback_rate": round(sum(r["fallback_count"] > 0 for r in records) / len(records), 4)}
            out.write("===== SUMMARY =====\n" + json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2)); print(f"output={OUTPUT}")


if __name__ == "__main__": asyncio.run(run())
