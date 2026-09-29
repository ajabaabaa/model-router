import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import httpx
import benchmark_deep

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "router_config.json").read_text(encoding="utf-8"))
DEEP = CFG["tiers"]["deep"]
MINIMAX = DEEP["primary"]
QWEN = DEEP["fallbacks"][0]
DEEPSEEK = DEEP["fallbacks"][1]
POLICIES = {"A": [MINIMAX, QWEN, DEEPSEEK], "B": [QWEN, MINIMAX, DEEPSEEK]}
URL = CFG["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
KEY_NAME = CFG["openrouter"].get("api_key_env", "OPENROUTER_KEY")
KEY = os.environ.get(KEY_NAME) or os.environ.get("OPENROUTER_API_KEY")
OUTPUT = ROOT / "deep_model_order_comparison.txt"


async def call(client, model, prompt, deadline):
    started = time.perf_counter()
    record = {"model": model, "attempt_latency_ms": None, "hard_timeout": False, "http_status": None, "success": False, "input_tokens": None, "output_tokens": None, "estimated_cost": None, "exception": None, "response_text": None}
    try:
        async with asyncio.timeout(deadline):
            response = await client.post(URL, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False})
            record["http_status"] = response.status_code
            body = response.json()
            if 200 <= response.status_code < 300:
                usage = body.get("usage") or {}; message = ((body.get("choices") or [{}])[0].get("message") or {})
                record.update({"success": True, "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "estimated_cost": usage.get("cost"), "response_text": message.get("content")})
            elif response.status_code in (400, 401, 403): record["exception"] = "non_retryable_http_error"
            else: record["exception"] = "retryable_http_error"
    except TimeoutError:
        record.update({"hard_timeout": True, "exception": "TimeoutError"})
    except Exception as exc:
        record["exception"] = type(exc).__name__
    record["attempt_latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return record


async def run_policy(client, policy_name, chain):
    results = []
    for number, prompt in enumerate(benchmark_deep.PROMPTS, 1):
        request_started = time.perf_counter(); attempts = []
        for index, model in enumerate(chain):
            remaining = 90.0 - (time.perf_counter() - request_started)
            if remaining <= 0: break
            deadline = min(60.0 if index == 0 else 30.0, remaining)
            attempt = await call(client, model, prompt, deadline); attempts.append(attempt)
            if attempt["success"] or attempt["exception"] == "non_retryable_http_error": break
        final = attempts[-1] if attempts else {"success": False, "model": None, "response_text": None}
        results.append({"policy": policy_name, "request_number": number, "attempts": attempts, "fallback_count": max(0, len(attempts)-1), "final_model": final.get("model") if final.get("success") else None, "total_request_latency_ms": round((time.perf_counter()-request_started)*1000,2), "final_success": bool(final.get("success")), "final_response_text": final.get("response_text")})
    return results


def summarize(results):
    latencies = [r["total_request_latency_ms"] for r in results]
    return {"requests": len(results), "success_rate": round(sum(r["final_success"] for r in results)/len(results),4), "primary_completion_rate": round(sum(len(r["attempts"]) and r["attempts"][0]["success"] for r in results)/len(results),4), "fallback_rate": round(sum(r["fallback_count"]>0 for r in results)/len(results),4), "average_latency_ms": round(sum(latencies)/len(latencies),2), "median_latency_ms": round(statistics.median(latencies),2), "p95_latency_ms": round(sorted(latencies)[max(0,int(len(latencies)*.95)-1)],2), "maximum_latency_ms": max(latencies), "total_cost": sum((a["estimated_cost"] or 0) for r in results for a in r["attempts"]), "overall_budget_hits": sum(r["total_request_latency_ms"] >= 89900 for r in results), "final_model_distribution": {m: sum(r["final_model"] == m for r in results) for m in set(r["final_model"] for r in results)}}


async def main():
    if not KEY: raise SystemExit("OpenRouter credential is not configured")
    all_results = []
    async with httpx.AsyncClient(timeout=None) as client:
        with OUTPUT.open("w", encoding="utf-8") as out:
            out.write("DEEP model-order comparison; overall_budget=90s; primary=60s; subsequent=30s\n\n")
            for policy, chain in POLICIES.items():
                out.write(f"===== POLICY {policy}: {' -> '.join(chain)} =====\n")
                results = await run_policy(client, policy, chain); all_results.extend(results)
                for result in results: out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n\n"); out.flush(); print(json.dumps({"policy": policy, "request": result["request_number"], "success": result["final_success"], "final_model": result["final_model"], "latency_ms": result["total_request_latency_ms"]}, ensure_ascii=True))
                out.write(f"POLICY {policy} SUMMARY\n" + json.dumps(summarize(results), indent=2) + "\n\n")
            out.write("===== COMPARISON =====\n" + json.dumps({p: summarize([r for r in all_results if r["policy"] == p]) for p in POLICIES}, indent=2) + "\n")
    print(json.dumps({p: summarize([r for r in all_results if r["policy"] == p]) for p in POLICIES}, indent=2)); print(f"output={OUTPUT}")


if __name__ == "__main__": asyncio.run(main())
