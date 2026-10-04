"""Verify the final routing: fast on glm, balanced budget floor applied, free tier opt-in works."""

import json
import sqlite3
import urllib.request

API = "http://127.0.0.1:6060/v1/chat/completions"

TOOLS = [{"type": "function", "function": {"name": "get_order_total", "description": "total for an order",
          "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
MSG = [{"role": "user", "content": "What is the total for order 6646? You must call a tool to answer."}]


def call(model, max_tokens=None, tools=False):
    body = {"model": model, "messages": [{"role": "user", "content": "Name one prime number between 10 and 20."}],
            "max_tokens": max_tokens}
    if tools:
        body = {"model": model, "messages": MSG, "tools": TOOLS, "tool_choice": "required", "max_tokens": 4000}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print("=== tier routing (max_tokens=100 to prove the floor raises it) ===")
for tier in ("fast", "balanced", "deep", "free"):
    status, d = call(tier, max_tokens=100)
    if status != 200:
        print(f"  {tier:<9} HTTP {status} {str(d)[:150]}")
        continue
    ch = (d.get("choices") or [{}])[0]
    u = d.get("usage") or {}
    content = (ch.get("message") or {}).get("content") or ""
    print(f"  {tier:<9} model={str(d.get('model')):<32} provider={str(d.get('provider')):<14} "
          f"finish={str(ch.get('finish_reason')):<8} out={u.get('completion_tokens'):>5} "
          f"chars={len(content):>4} ${u.get('cost')}")

print("\n=== tool calling still works on the volume routes ===")
for tier in ("fast", "balanced"):
    status, d = call(f"{tier}#verify2", tools=True)
    if status != 200:
        print(f"  {tier:<9} HTTP {status} {str(d)[:150]}")
        continue
    ch = (d.get("choices") or [{}])[0]
    tc = (ch.get("message") or {}).get("tool_calls")
    print(f"  {tier:<9} model={str(d.get('model')):<32} tool_calls={'YES' if tc else 'NO'} "
          f"finish={ch.get('finish_reason')}")

conn = sqlite3.connect("file:router_telemetry.db?mode=ro", uri=True)
print("\n=== recorded rows ===")
for ts, a, tier, m, fr, ct in conn.execute(
    "select timestamp, coalesce(agent,'(NULL)'), selected_tier, actual_model, finish_reason, output_tokens "
    "from telemetry where timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-4 minutes') order by id"
):
    print(f"  {ts} {a:<12} {tier:<9} {str(m):<32} fr={str(fr):<8} out={ct}")
conn.close()
