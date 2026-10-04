"""Live verification through the running router on :6060.

Part 1 - each traffic tier must serve under its governance gate and cost near zero.
Part 2 - negative control on the inert `jev` tier: give it a model + gate that must 404, proving
         the policy in router_config.json is actually sent upstream by the live process.
"""

import json
import sqlite3
import urllib.error
import urllib.request

API = "http://127.0.0.1:6060/v1/chat/completions"
CONFIG = "router_config.json"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order_total",
            "description": "Return the total for an order id.",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        },
    }
]
MSG = [{"role": "user", "content": "What is the total for order 6646? You must call a tool to answer."}]


def call(model, agent):
    body = {"model": model, "messages": MSG, "tools": TOOLS, "tool_choice": "required", "max_tokens": 2000}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-OpenClaw-Agent": agent}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


print("=== A. live tier calls through :6060 ===")
for tier in ("fast", "balanced", "deep"):
    status, d = call(f"router-local/{tier}#verify", "verify")
    if status != 200:
        print(f"  {tier:<9} HTTP {status}  {d}")
        continue
    ch = (d.get("choices") or [{}])[0]
    u = d.get("usage") or {}
    print(
        f"  {tier:<9} HTTP 200  model={d.get('model'):<34} provider={str(d.get('provider')):<14} "
        f"finish={ch.get('finish_reason'):<11} tool_calls={'Y' if ch.get('message', {}).get('tool_calls') else 'N'} "
        f"pt={u.get('prompt_tokens')} ct={u.get('completion_tokens')} cost=${u.get('cost')}"
    )

print("\n=== B. NEGATIVE CONTROL - jev given a model that must fail under zdr ===")
original = open(CONFIG, encoding="utf-8").read()
try:
    cfg = json.loads(original)
    cfg["tiers"]["jev"] = {
        "provider": "openrouter",
        "primary": "qwen/qwen3.7-max",
        "fallbacks": [],
        "provider_policy": {"zdr": True},
    }
    open(CONFIG, "w", encoding="utf-8", newline="\n").write(json.dumps(cfg, indent=2) + "\n")
    status, d = call("router-local/jev#verify", "verify")
    blocked = isinstance(d, str) and "data policy" in d
    print(f"  jev with qwen3.7-max + zdr:true -> HTTP {status} "
          f"{'BLOCKED AS PREDICTED (policy is live-enforced)' if blocked else 'UNEXPECTED: ' + str(d)[:200]}")
finally:
    open(CONFIG, "w", encoding="utf-8", newline="\n").write(original)
    print("  jev tier restored; config is byte-identical to before the control"
          if open(CONFIG, encoding="utf-8").read() == original else "  RESTORE FAILED - CHECK CONFIG BY HAND")

status, d = call("router-local/deep#verify", "verify")
print(f"  post-restore deep -> HTTP {status} model={d.get('model') if status == 200 else str(d)[:120]}")

print("\n=== C. telemetry rows recorded by the live service ===")
conn = sqlite3.connect("file:router_telemetry.db?mode=ro", uri=True)
for ts, tier, model, fr, cost, fb, outcomes in conn.execute(
    "select timestamp, selected_tier, actual_model, finish_reason, estimated_cost, fallback_count, attempt_outcomes "
    "from telemetry where agent='verify' order by id desc limit 8"
):
    print(f"  {ts}  {tier:<9} {str(model):<34} fr={str(fr):<11} cost=${cost} fallbacks={fb}  {str(outcomes)[:120]}")
conn.close()
