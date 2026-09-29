import json
import time
import statistics
import urllib.request
import urllib.error
from pathlib import Path


ENDPOINT = "http://127.0.0.1:6060/v1/chat/completions"
OUTPUT = Path(__file__).with_name("benchmark_deep.txt")

PROMPTS = [
    "Explain SQLite vs PostgreSQL in under 150 words.",
    "Explain SSE streaming in under 150 words.",
    "Design a simple retry strategy for an API.\nInclude:\n- what errors should be retried\n- what errors should not be retried\n- backoff approach\n- maximum attempts",
    "Explain idempotency in APIs in under 150 words.\nGive one concrete example.",
    "List five useful LLM router metrics and explain why each matters.",
    "Compare polling versus webhooks for an agent workflow.\nCover:\n- latency\n- reliability\n- infrastructure complexity\n- cost\n- best use case for each",
    "Propose a simple database schema for storing LLM routing telemetry.\nInclude the main tables and the most important fields.",
    "Explain the difference between:\n- request timeout\n- network error\n- model-unavailable error\n\nFor each, explain how an LLM router should respond.",
    "Design a safe fallback strategy for a three-tier LLM router:\nFAST → BALANCED → DEEP\n\nExplain:\n- when to escalate\n- when not to retry\n- how to prevent runaway cost\n- what telemetry should be recorded",
    'Review this routing strategy:\n\n"Use a cheap model first and escalate to a stronger model only when confidence is low."\n\nGive:\n- advantages\n- disadvantages\n- likely failure modes\n- situations where this strategy works well\n- situations where it should not be used',
]


def call(prompt):
    payload = json.dumps({
        "model": "deep",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode("utf-8")
            status = response.status
        body = json.loads(raw)
        usage = body.get("usage") or {}
        return {
            "success": 200 <= status < 300,
            "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "actual_model": body.get("model"),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "estimated_cost": body.get("estimated_cost", usage.get("cost")),
            "fallback_count": body.get("fallback_count", 0),
            "response": body,
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {
            "success": False,
            "status": exc.code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": raw,
        }
    except Exception as exc:
        return {
            "success": False,
            "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main():
    results = []
    with OUTPUT.open("w", encoding="utf-8") as out:
        out.write("DEEP benchmark\n")
        out.write(f"endpoint={ENDPOINT}\nmodel=deep\nrequests={len(PROMPTS)}\n\n")
        for index, prompt in enumerate(PROMPTS, 1):
            result = call(prompt)
            results.append(result)
            out.write(f"===== REQUEST {index} =====\n")
            out.write(json.dumps({"prompt": prompt, **result}, ensure_ascii=False, indent=2))
            out.flush()
            print(json.dumps({"request": index, **result}, ensure_ascii=True))
            out.write("\n\n")
            out.flush()
        successes = [r for r in results if r["success"]]
        out.write("===== SUMMARY =====\n")
        summary = {
            "requests_attempted": len(results),
            "successes": len(successes),
            "failures": len(results) - len(successes),
            "actual_models": [r.get("actual_model") for r in results],
            "average_latency_ms_successful": round(sum(r["latency_ms"] for r in successes) / len(successes), 2) if successes else None,
            "median_latency_ms": round(statistics.median(r["latency_ms"] for r in results), 2) if results else None,
            "maximum_latency_ms": max((r["latency_ms"] for r in results), default=None),
            "over_30_seconds": [i + 1 for i, r in enumerate(results) if r["latency_ms"] > 30000],
            "over_60_seconds": [i + 1 for i, r in enumerate(results) if r["latency_ms"] > 60000],
            "over_120_seconds": [i + 1 for i, r in enumerate(results) if r["latency_ms"] > 120000],
            "total_input_tokens": sum(r.get("input_tokens") or 0 for r in results),
            "total_output_tokens": sum(r.get("output_tokens") or 0 for r in results),
            "total_estimated_cost": sum(r.get("estimated_cost") or 0 for r in results),
            "fallback_count": sum(r.get("fallback_count") or 0 for r in results),
        }
        out.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output={OUTPUT}")


if __name__ == "__main__":
    main()
