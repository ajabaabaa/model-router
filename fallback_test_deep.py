import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "router_config.json").read_text(encoding="utf-8"))
OUTPUT = ROOT / "fallback_test_deep.txt"
PROMPTS = [
    "Explain SSE streaming in under 150 words.",
    "Design a safe retry strategy for an API.",
    "Compare polling and webhooks.",
    "Propose an LLM telemetry schema.",
    "Explain safe fallback for a three-tier LLM router.",
]
CHAIN = [CONFIG["tiers"]["deep"]["primary"], *CONFIG["tiers"]["deep"]["fallbacks"]]
BASE_URL = CONFIG["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
KEY_NAME = CONFIG["openrouter"].get("api_key_env", "OPENROUTER_KEY")
API_KEY = os.environ.get(KEY_NAME) or os.environ.get("OPENROUTER_API_KEY")


def attempt(model, prompt, timeout):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    result = {"model": model, "latency_ms": None, "status": None, "success": False}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            result["status"] = response.status
        payload = json.loads(raw)
        usage = payload.get("usage") or {}
        result.update({
            "success": 200 <= result["status"] < 300,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "estimated_cost": usage.get("cost"),
        })
    except urllib.error.HTTPError as exc:
        result.update({"status": exc.code, "exception_class": type(exc).__name__})
    except Exception as exc:
        result.update({"exception_class": type(exc).__name__, "error": str(exc)[:200]})
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def main():
    if not API_KEY:
        raise SystemExit("OpenRouter credential is not configured")
    results = []
    with OUTPUT.open("w", encoding="utf-8") as out:
        out.write("Temporary DEEP fallback diagnostic\n")
        out.write(f"chain={json.dumps(CHAIN)}\nprimary_timeout_seconds=10\n\n")
        for number, prompt in enumerate(PROMPTS, 1):
            started = time.perf_counter()
            attempts = []
            for index, model in enumerate(CHAIN):
                result = attempt(model, prompt, 10 if index == 0 else 60)
                attempts.append(result)
                if result["success"]:
                    break
            final = attempts[-1]
            record = {
                "request_number": number,
                "primary_model_attempted": CHAIN[0],
                "primary_timed_out": attempts[0].get("exception_class") == "TimeoutError",
                "fallback_model_used": final["model"] if len(attempts) > 1 else None,
                "final_http_status": final.get("status"),
                "final_success": final.get("success", False),
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "attempts": attempts,
                "fallback_count": max(0, len(attempts) - 1),
                "exception_class": final.get("exception_class"),
            }
            results.append(record)
            out.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n\n")
            out.flush()
        summary = {
            "requests_attempted": len(results),
            "successes": sum(r["final_success"] for r in results),
            "failures": sum(not r["final_success"] for r in results),
            "fallbacks_used": sum(r["fallback_count"] for r in results),
            "final_models": [r["fallback_model_used"] or r["primary_model_attempted"] for r in results],
            "total_input_tokens_by_attempt": sum(a.get("input_tokens") or 0 for r in results for a in r["attempts"]),
            "total_output_tokens_by_attempt": sum(a.get("output_tokens") or 0 for r in results for a in r["attempts"]),
            "total_estimated_cost_by_attempt": sum(a.get("estimated_cost") or 0 for r in results for a in r["attempts"]),
        }
        out.write("===== SUMMARY =====\n" + json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"output={OUTPUT}")


if __name__ == "__main__":
    main()
