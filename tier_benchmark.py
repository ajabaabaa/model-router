import asyncio
import argparse
import json
import random
import re
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent
URL = "http://127.0.0.1:6060/v1/chat/completions"
OUTPUT = ROOT / "tier_benchmark_results.txt"
BLIND_V8_OUTPUT = ROOT / "tier_quality_v8_blind.md"
BLIND_V8_KEY = ROOT / "tier_quality_v8_blind_key.json"
RUN_ID = "tier-benchmark-v8-interactive-quality"
CLIENT_TIMEOUT_SECONDS = 105
TOTAL_BUDGET_MS = 90000
PRIMARY_ATTEMPT_MS = 60000
TIERS = ["fast", "balanced"]
PROMPTS = [
    "Design a production architecture for a multi-tenant event-driven platform with synchronous APIs, async workflows, tenant isolation, regional failover, auditability, and zero-downtime deploys. State assumptions, boundaries, data flows, and the hardest tradeoffs.",
    "Diagnose a service that has rising p99 latency, intermittent 502s, duplicate jobs, database lock contention, and missing traces after a recent deployment. Build a hypothesis tree, distinguish correlated from causal symptoms, specify evidence to collect, and propose a safe remediation order.",
    "Design a document-processing system constrained to: immutable raw inputs, GDPR deletion, seven-year audit retention, sub-minute status updates, at-least-once delivery, bounded storage cost, and tenant-specific encryption keys. Explain conflicts and an implementable design.",
    "Review this security-sensitive pattern: an API accepts a signed webhook, stores the event, immediately publishes its user-supplied URL to a worker, and marks the event processed after a 200 response. Identify subtle correctness, replay, SSRF, authorization, idempotency, and observability issues, then give a corrected sequence.",
    "Design an agent workflow that can plan, call tools, request approval for risky actions, recover from partial failure, and resume after process restart. Define state, invariants, idempotency, compensation, human checkpoints, and what must never be inferred silently.",
    "Compare three designs for an LLM feature under a strict monthly budget: one large model, cheap-first escalation, and parallel small-model voting. Analyze expected cost, tail latency, failure modes, quality variance, capacity planning, and when each dominates.",
    "Propose a data model for orders, payments, refunds, shipment events, and customer-visible status when events can arrive late, duplicated, reordered, or corrected. Preserve audit history while supporting fast current-state queries and safe reconciliation.",
    "Synthesize a practical technical strategy for operating an OpenAI-compatible model gateway: request validation, streaming, provider errors, fallbacks, deadlines, telemetry without sensitive content, cost attribution, and incident response. Identify where naive implementations fail.",
    "A team says: 'The cache is stale, so disable caching; the queue is backed up, so add workers; errors increased after the database migration, so roll back.' Decompose the problem without accepting these conclusions. Explain what is ambiguous, what evidence separates hypotheses, and how to choose low-risk experiments.",
    "Plan a six-step migration from a monolith with a shared database to services with independent deploys. Include dependency ordering, dual writes or alternatives, backfill verification, rollback boundaries, contract compatibility, observability gates, and exit criteria.",
    "Perform a failure-mode analysis for a payment-triggered fulfillment workflow involving an API gateway, queue, payment provider, inventory service, and email provider. Cover timeouts, retries, duplicate delivery, partial commits, provider ambiguity, operator intervention, and customer-visible truth.",
    "Plan migration of a public REST API to versioned contracts and asynchronous operations while preserving existing clients. Address schema evolution, idempotency, pagination, error compatibility, deprecation, traffic shadowing, data backfills, and rollback.",
    "Design observability for a distributed agent and LLM gateway system. Specify metrics, logs, traces, exemplars, cardinality controls, privacy boundaries, SLOs, alert thresholds, cost telemetry, and how to distinguish provider, router, client, and workload failures.",
    "Design a tiered LLM routing policy for FAST, BALANCED, and DEEP using capability requirements, uncertainty, deadlines, budgets, provider health, and fallback safety. Prevent runaway escalation and explain how to evaluate whether routing adds value over strong-model and equal-budget baselines.",
    "Evaluate this claim: 'If an operation is idempotent, it is always safe to retry, and if a request returned HTTP 200, the business operation definitely succeeded.' Identify hidden assumptions and counterexamples involving timeouts, streaming, asynchronous processing, proxies, payment APIs, and eventual consistency.",
]

def extract_response_content(body):
    choices = body.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    candidates = [
        ("message.content", message.get("content")),
        ("message.reasoning_content", message.get("reasoning_content")),
        ("message.reasoning", message.get("reasoning")),
        ("choice.text", choice.get("text")),
        ("output_text", body.get("output_text")),
    ]
    for source, value in candidates:
        if isinstance(value, str) and value.strip():
            return value, source
    return None, None


async def run_one(client, tier, number, prompt):
    started = time.perf_counter()
    result = {"benchmark_run_id": RUN_ID, "tier": tier, "prompt_number": number, "success": False, "actual_model": None, "latency_ms": None, "input_tokens": None, "output_tokens": None, "estimated_cost": None, "answer": None, "response_text": None, "final_content_present": False, "content_character_count": 0, "reasoning_character_count": 0, "response_text_source": None, "message_content": None, "message_reasoning": None, "message_reasoning_content": None, "assistant_message": None, "choice": None, "output_text": None, "exception": None, "http_status": None, "attempted_models": None, "budget_exhausted": None, "remaining_budget_ms": None, "finish_reason": None, "fallback_count": None}
    budgets = [60000, 30000]
    request_id = f"{RUN_ID}-{tier}-{number}"
    request_prompt = prompt + "\n\nProvide a concise final answer. Prioritize the final answer over extended reasoning."
    result["prompt"] = request_prompt
    payload = {"model": tier, "messages": [{"role": "user", "content": request_prompt}], "stream": False, "max_tokens": 2048}
    payload["reasoning"] = {"effort": "none" if tier == "fast" else "minimal"}
    try:
        response = await client.post(URL, json=payload, headers={"Content-Type": "application/json", "X-Router-Total-Budget-Ms": str(TOTAL_BUDGET_MS), "X-Router-Benchmark-Attempt-Budgets-Ms": ",".join(map(str, budgets)), "X-Request-ID": request_id})
        result["http_status"] = response.status_code
        body = response.json()
        result["actual_model"] = body.get("model")
        usage = body.get("usage") or {}
        result["input_tokens"] = usage.get("prompt_tokens")
        result["output_tokens"] = usage.get("completion_tokens")
        result["estimated_cost"] = usage.get("cost") or body.get("estimated_cost")
        result["fallback_count"] = body.get("fallback_count", 0)
        result["attempted_models"] = body.get("attempted_models")
        result["budget_exhausted"] = body.get("budget_exhausted")
        result["remaining_budget_ms"] = body.get("remaining_budget_ms")
        choices = body.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else None
        message = choice.get("message") if isinstance(choice, dict) and isinstance(choice.get("message"), dict) else None
        result["choice"] = choice
        result["assistant_message"] = message
        result["message_content"] = message.get("content") if message else None
        result["message_reasoning"] = message.get("reasoning") if message else None
        result["message_reasoning_content"] = message.get("reasoning_content") if message else None
        result["output_text"] = body.get("output_text")
        result["finish_reason"] = choice.get("finish_reason") if choice else None
        result["response_text"] = result["message_content"] if isinstance(result["message_content"], str) and result["message_content"].strip() else None
        result["response_text_source"] = "message.content" if result["response_text"] else None
        result["final_content_present"] = bool(result["response_text"])
        result["content_character_count"] = len(result["message_content"]) if isinstance(result["message_content"], str) else 0
        result["reasoning_character_count"] = sum(len(v) for v in (result["message_reasoning"], result["message_reasoning_content"]) if isinstance(v, str))
        result["answer"] = result["response_text"]
        result["success"] = 200 <= response.status_code < 300
    except TimeoutError:
        result["exception"] = "TimeoutError"
    except Exception as exc:
        result["exception"] = type(exc).__name__
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def summary(rows):
    if not rows:
        return {"requests": 0, "success_rate": None, "average_latency_ms": None, "median_latency_ms": None, "maximum_latency_ms": None, "total_input_tokens": 0, "total_output_tokens": 0, "total_cost": 0, "fallback_count": 0}
    latencies = [r["latency_ms"] for r in rows]
    return {"requests": len(rows), "success_rate": round(sum(r["success"] for r in rows) / len(rows), 4), "average_latency_ms": round(sum(latencies) / len(latencies), 2), "median_latency_ms": round(statistics.median(latencies), 2), "maximum_latency_ms": max(latencies), "total_input_tokens": sum(r["input_tokens"] or 0 for r in rows), "total_output_tokens": sum(r["output_tokens"] or 0 for r in rows), "total_cost": sum(r["estimated_cost"] or 0 for r in rows), "fallback_count": sum(r["fallback_count"] or 0 for r in rows)}


def existing_rows():
    if not OUTPUT.exists():
        return []
    text = OUTPUT.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder(); rows = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "tier" in value and "prompt_number" in value:
            rows.append(value)
    return rows

def missing_prompts(rows, tier, prompt_number=None):
    completed = {(r.get("benchmark_run_id"), r.get("tier"), r.get("prompt_number")) for r in rows}
    return [(number, prompt) for number, prompt in enumerate(PROMPTS, 1) if (RUN_ID, tier, number) not in completed and (prompt_number is None or number == prompt_number)]

def create_v8_blind_package():
    rows = [r for r in existing_rows() if r.get("benchmark_run_id") == RUN_ID]
    by_key = {(r.get("tier"), r.get("prompt_number")): r for r in rows}
    expected = {(tier, number) for tier in TIERS for number in range(1, 16)}
    if set(by_key) != expected:
        raise ValueError("v8 requires exactly one result row per tier and prompt")
    sections = []
    key = {}
    for number, prompt in enumerate(PROMPTS, 1):
        pair = [(tier, by_key[(tier, number)].get("message_content")) for tier in TIERS]
        if any(not isinstance(content, str) or not content.strip() for _, content in pair):
            raise ValueError(f"missing final message.content for prompt {number}")
        random.SystemRandom().shuffle(pair)
        key[str(number)] = {label: tier for label, (tier, _) in zip(("A", "B"), pair)}
        sections.append(f"# Prompt {number}\n\n## Original prompt\n\n{prompt}\n\n## Response A\n\n{pair[0][1]}\n\n## Response B\n\n{pair[1][1]}\n")
    document = "\n---\n\n".join(sections)
    BLIND_V8_OUTPUT.write_text(document, encoding="utf-8")
    BLIND_V8_KEY.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_v8_blind_package(document, key)
    return {"prompts": len(sections), "responses": len(sections) * 2, "blind_file": str(BLIND_V8_OUTPUT), "key_file": str(BLIND_V8_KEY)}

def verify_v8_blind_package(document, key):
    if len(key) != 15 or document.count("## Response A") != 15 or document.count("## Response B") != 15:
        raise ValueError("blind package must have 15 prompts and 30 responses")
    if any(not isinstance(v, str) or not v.strip() for r in existing_rows() if r.get("benchmark_run_id") == RUN_ID for v in [r.get("message_content")]):
        raise ValueError("v8 contains missing final response content")
    # Check response identity and benchmark metadata separately from prompt
    # text and ordinary technical prose (which may discuss latency or costs).
    response_sections = document.split("## Response ")[1:]
    for section in response_sections:
        if any(name.casefold() in section.casefold() for name in ("qwen/qwen3.8-flash", "qwen/qwen3.8-max-0902", "minimax/minimax-m3", "openrouter.ai")):
            raise ValueError("actual model/provider identity leaked into a response section")
        if re.search(r"Response\s+[AB]\s*(?:is|was|=|:)\s*(?:FAST|BALANCED)\b", section, re.I):
            raise ValueError("response-to-tier identity mapping leaked")
    if any(field in document for field in ("benchmark_run_id", "actual_model:", "latency_ms:", "estimated_cost:", "output_tokens:", "fallback_count:", "attempted_models:", "budget_exhausted:")):
        raise ValueError("benchmark metadata leaked into the blind document")


async def main(tier, prompt_number=None):
    rows = existing_rows()
    missing = missing_prompts(rows, tier, prompt_number)
    print(f"{tier.upper()}: {len(PROMPTS) - len([x for x in missing])}/15 completed before run; {len(missing)} remaining")
    async with httpx.AsyncClient(timeout=CLIENT_TIMEOUT_SECONDS) as client:
        with OUTPUT.open("a", encoding="utf-8") as out:
            if not OUTPUT.stat().st_size:
                out.write("Tier benchmark; 15 prompts; 90-second wall-clock budget per request\n\n")
            for number, prompt in missing:
                row = await run_one(client, tier, number, prompt); rows.append(row)
                out.write(json.dumps({"prompt": prompt, **row}, ensure_ascii=False, indent=2) + "\n\n"); out.flush()
                print(json.dumps({k: row[k] for k in ("tier", "prompt_number", "success", "actual_model", "latency_ms", "exception")}, ensure_ascii=True))
    tier_rows = [r for r in rows if r.get("benchmark_run_id") == RUN_ID and r.get("tier") == tier]
    print(f"{tier.upper()} SUMMARY\n" + json.dumps({"completed_requests": len(tier_rows), **summary(tier_rows)}, indent=2))


def status():
    rows = existing_rows(); counts = {tier: len({r.get("prompt_number") for r in rows if r.get("benchmark_run_id") == RUN_ID and r.get("tier") == tier}) for tier in TIERS}
    legacy = {tier: sum(1 for r in rows if not r.get("benchmark_run_id") and r.get("tier") == tier) for tier in TIERS}
    other_runs = {tier: sum(1 for r in rows if r.get("benchmark_run_id") and r.get("benchmark_run_id") != RUN_ID and r.get("tier") == tier) for tier in TIERS}
    print(f"CURRENT RUN: {RUN_ID}")
    for tier in TIERS:
        print(f"{tier.upper()}: {counts[tier]}/15 completed")
    print("PRIOR RUNS:")
    print(f"v3 (tier-benchmark-v3-budgeted): {sum(1 for r in rows if r.get('benchmark_run_id') == 'tier-benchmark-v3-budgeted')} rows")
    print(f"legacy/no-run-id: {sum(legacy.values())} rows")
    if any(other_runs.values()):
        print("OTHER RUNS (excluded):")
        for tier in TIERS:
            print(f"{tier.upper()}: {other_runs[tier]} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); group = parser.add_mutually_exclusive_group(required=True); group.add_argument("--tier", choices=TIERS); group.add_argument("--status", action="store_true"); group.add_argument("--create-v8-blind", action="store_true"); parser.add_argument("--prompt-number", type=int, choices=range(1, 16)); args = parser.parse_args()
    if args.status: status()
    elif args.create_v8_blind: print(json.dumps(create_v8_blind_package(), indent=2))
    else: asyncio.run(main(args.tier, args.prompt_number))
