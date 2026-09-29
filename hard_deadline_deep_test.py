import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import httpx
import benchmark_deep


ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "router_config.json").read_text(encoding="utf-8"))
CHAIN = [CONFIG["tiers"]["deep"]["primary"], *CONFIG["tiers"]["deep"]["fallbacks"]]
URL = CONFIG["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
KEY_NAME = CONFIG["openrouter"].get("api_key_env", "OPENROUTER_KEY")
KEY = os.environ.get(KEY_NAME) or os.environ.get("OPENROUTER_API_KEY")
OUTPUT = ROOT / "hard_deadline_deep_test.txt"


async def attempt(client, model, prompt, attempt_number, deadline):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
    started = time.perf_counter()
    record = {"model": model, "attempt_number": attempt_number, "outcome": "failure", "http_status": None, "hard_timeout": False, "input_tokens": None, "output_tokens": None, "estimated_cost": None, "exception": None}
    try:
        async with asyncio.timeout(deadline):
            response = await client.post(URL, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, json=payload)
            record["http_status"] = response.status_code
            if 200 <= response.status_code < 300:
                body = response.json(); usage = body.get("usage") or {}
                record.update({"outcome": "success", "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "estimated_cost": usage.get("cost")})
            elif response.status_code in (400, 401, 403):
                record["outcome"] = "non_retryable_failure"
            else:
                record["outcome"] = "retryable_failure"
    except TimeoutError:
        record.update({"outcome": "hard_timeout", "hard_timeout": True, "exception": "TimeoutError"})
    except Exception as exc:
        record.update({"exception": type(exc).__name__, "outcome": "retryable_failure"})
    record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return record


async def run():
    if not KEY:
        raise SystemExit("OpenRouter credential is not configured")
    records = []
    async with httpx.AsyncClient(timeout=None) as client:
        with OUTPUT.open("w", encoding="utf-8") as out:
            out.write(f"chain={json.dumps(CHAIN)}\n deadlines=[60,45,45]\n\n")
            for number, prompt in enumerate(benchmark_deep.PROMPTS, 1):
                started = time.perf_counter(); attempts = []
                for index, model in enumerate(CHAIN):
                    attempts.append(await attempt(client, model, prompt, index + 1, 60 if index == 0 else 45))
                    if attempts[-1]["outcome"] in ("success", "non_retryable_failure"): break
                final = attempts[-1]
                request_record = {"request_number": number, "attempts": attempts, "final_success": final["outcome"] == "success", "final_model": final["model"] if final["outcome"] == "success" else None, "total_wall_clock_latency_ms": round((time.perf_counter() - started) * 1000, 2), "fallback_count": len(attempts) - 1, "total_estimated_cost": sum(a["estimated_cost"] or 0 for a in attempts)}
                records.append(request_record); out.write(json.dumps(request_record, indent=2) + "\n\n"); out.flush(); print(json.dumps(request_record, ensure_ascii=True))
            latencies = [r["total_wall_clock_latency_ms"] for r in records]
            summary = {"requests_attempted": len(records), "successes": sum(r["final_success"] for r in records), "failures": sum(not r["final_success"] for r in records), "minimax_completions": sum(r["attempts"][0]["outcome"] == "success" for r in records), "minimax_hard_timeouts": sum(r["attempts"][0]["hard_timeout"] for r in records), "qwen_recoveries": sum(r["final_model"] == CHAIN[1] for r in records), "deepseek_recoveries": sum(r["final_model"] == CHAIN[2] for r in records), "unrecovered_requests": sum(not r["final_success"] for r in records), "average_total_latency_ms": round(sum(latencies) / len(latencies), 2), "median_total_latency_ms": round(statistics.median(latencies), 2), "maximum_total_latency_ms": max(latencies), "total_cost": sum(r["total_estimated_cost"] for r in records), "fallback_rate": round(sum(r["fallback_count"] > 0 for r in records) / len(records), 4)}
            out.write("===== SUMMARY =====\n" + json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2)); print(f"output={OUTPUT}")


if __name__ == "__main__": asyncio.run(run())
