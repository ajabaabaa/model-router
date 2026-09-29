import json
import os
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import benchmark_deep

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "router_config.json").read_text(encoding="utf-8"))
CHAIN = [CFG["tiers"]["deep"]["primary"], *CFG["tiers"]["deep"]["fallbacks"]]
URL = CFG["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
KEY_NAME = CFG["openrouter"].get("api_key_env", "OPENROUTER_KEY")
KEY = os.environ.get(KEY_NAME) or os.environ.get("OPENROUTER_API_KEY")
OUT = ROOT / "timeout_experiment_deep.txt"


def call(model, prompt, timeout):
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}).encode()
    req = urllib.request.Request(URL, data=payload, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    started = time.perf_counter()
    result = {"model": model, "success": False, "status": None}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read())
            result["status"] = response.status
        usage = data.get("usage") or {}
        result.update({"success": 200 <= result["status"] < 300, "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"), "estimated_cost": usage.get("cost")})
    except urllib.error.HTTPError as exc:
        result.update({"status": exc.code, "exception_class": type(exc).__name__})
    except Exception as exc:
        result.update({"exception_class": type(exc).__name__, "error": str(exc)[:200]})
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def main():
    if not KEY:
        raise SystemExit("OpenRouter credential is not configured")
    records = []
    with OUT.open("w", encoding="utf-8") as out:
        out.write(f"chain={json.dumps(CHAIN)}\nprimary_timeout_seconds=90\nfallback_timeout_seconds=120\n\n")
        for number, prompt in enumerate(benchmark_deep.PROMPTS, 1):
            started = time.perf_counter(); attempts = []
            for index, model in enumerate(CHAIN):
                result = call(model, prompt, 90 if index == 0 else 120)
                attempts.append(result)
                if result["success"]: break
            final = attempts[-1]
            record = {"request_number": number, "primary_model": CHAIN[0], "primary_completed": attempts[0]["success"], "primary_timed_out": attempts[0].get("exception_class") == "TimeoutError", "fallback_model": final["model"] if len(attempts) > 1 else None, "final_model": final["model"], "success": final["success"], "total_latency_ms": round((time.perf_counter()-started)*1000,2), "minimax_attempt_latency_ms": attempts[0]["latency_ms"], "fallback_latency_ms": round(sum(a["latency_ms"] for a in attempts[1:]),2) if len(attempts)>1 else None, "fallback_count": len(attempts)-1, "attempts": attempts}
            records.append(record); out.write(json.dumps(record, indent=2) + "\n\n"); out.flush(); print(json.dumps(record, ensure_ascii=True))
        latencies=[r["total_latency_ms"] for r in records]
        summary={"requests_attempted":len(records),"successes":sum(r["success"] for r in records),"failures":sum(not r["success"] for r in records),"miniMax_completed":sum(r["primary_completed"] for r in records),"qwen_recovered":sum(r["final_model"]==CHAIN[1] for r in records),"deepseek_required":sum(r["final_model"]==CHAIN[2] for r in records),"average_latency_ms":round(sum(latencies)/len(latencies),2),"median_latency_ms":round(statistics.median(latencies),2),"maximum_latency_ms":max(latencies),"total_cost":sum((a.get("estimated_cost") or 0) for r in records for a in r["attempts"]),"fallback_rate":round(sum(r["fallback_count"]>0 for r in records)/len(records),4)}
        out.write("===== SUMMARY =====\n"+json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2)); print(f"output={OUT}")


if __name__ == "__main__": main()
