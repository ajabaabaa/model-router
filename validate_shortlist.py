"""Final gate + tool-call validation for the exact shortlist I intend to ship."""

import json
import os
import urllib.request

CHAT = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")

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

SHORTLIST = [
    "deepseek/deepseek-v4.1-flash",
    "~deepseek/deepseek-flash-latest",
    "deepseek/deepseek-v4-flash",
    "~z-ai/glm-flash-latest",
    "minimax/minimax-m3",
    "qwen/qwen3.7-max",
    "anthropic/claude-opus-5.5",
    "qwen/qwen3.8-27b:free",
    "openrouter/free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "thinkingmachines/inkling:free",
    "stealth/space-bunny-alpha",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.1-flash",
    "poolside/laguna-s-2.1:free",
]
GATES = [("open", None), ("deny", {"data_collection": "deny"}), ("zdr", {"zdr": True})]


def post(body):
    req = urllib.request.Request(
        CHAT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:6060",
            "X-Title": "openclaw-router-validation",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, (e.code, e.read().decode()[:200])
    except Exception as e:
        return None, (0, f"{type(e).__name__}: {e}")


total = 0.0
print(f"{'model':<46}{'gate':>6}  result")
for mid in SHORTLIST:
    for gname, gate in GATES:
        body = {"model": mid, "messages": MSG, "tools": TOOLS, "tool_choice": "required", "max_tokens": 48}
        if gate:
            body["provider"] = gate
        d, err = post(body)
        if err:
            code, detail = err
            kind = "ZDR-BLOCK" if "data policy" in detail or "Zero data" in detail else (
                "429-LIMIT" if code == 429 or "temporar" in detail else f"ERR{code}"
            )
            print(f"{mid:<46}{gname:>6}  {kind} {detail[:80]}")
            continue
        ch = (d.get("choices") or [{}])[0]
        u = d.get("usage") or {}
        cost = float(u.get("cost") or 0)
        total += cost
        tc = bool(ch.get("message", {}).get("tool_calls"))
        print(
            f"{mid:<46}{gname:>6}  {'TOOL-OK' if tc else 'NO-TOOL-CALL'} "
            f"prov={d.get('provider')} finish={ch.get('finish_reason')} pt={u.get('prompt_tokens')} ${cost:.6f}"
        )

print(f"\ntotal probe spend this run: ${total:.6f}")
