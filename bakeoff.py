"""Bake-off: which ZDR-eligible route actually produces answers on the 15 quality prompts?

Adds temporary candidate tiers to router_config.json (re-read per request, so no restart),
runs the same prompts the v8 baseline used, then restores the config byte-for-byte.
"""

import asyncio
import json
import time

import httpx

import tier_benchmark as tb

CONFIG = "router_config.json"
URL = "http://127.0.0.1:6060/v1/chat/completions"
CANDIDATES = {
    "cand_glm": "~z-ai/glm-flash-latest",
    "cand_m3": "minimax/minimax-m3",
    "cand_dsflash": "deepseek/deepseek-v4-flash",
}
MAX_TOKENS = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 8192
# Optional second arg, e.g. "cand_v41=deepseek/deepseek-v4.1-flash", to re-run one route
# (such as the incumbent primary) at an equal token budget for a fair comparison.
if len(__import__("sys").argv) > 2:
    CANDIDATES = dict(item.split("=", 1) for item in __import__("sys").argv[2].split(","))
CONCURRENCY = 4

original = open(CONFIG, encoding="utf-8").read()


def add_tiers():
    cfg = json.loads(original)
    for name, model in CANDIDATES.items():
        cfg["tiers"][name] = {"provider": "openrouter", "primary": model, "fallbacks": [],
                              "provider_policy": {"zdr": True}}
    open(CONFIG, "w", encoding="utf-8", newline="\n").write(json.dumps(cfg, indent=2) + "\n")


def restore():
    open(CONFIG, "w", encoding="utf-8", newline="\n").write(original)


async def one(client, tier, model, number, prompt):
    payload = {"model": tier, "messages": [{"role": "user", "content": prompt}],
               "stream": False, "max_tokens": MAX_TOKENS, "reasoning": {"effort": "minimal"}}
    out = {"tier": tier, "want": model, "prompt_number": number, "ok": False, "model": None,
           "chars": 0, "out_tok": 0, "cost": 0.0, "finish": None, "latency_s": None, "exc": None}
    started = time.perf_counter()
    try:
        r = await client.post(URL, json=payload, headers={"Content-Type": "application/json"})
        b = r.json()
        ch = (b.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content = msg.get("content")
        u = b.get("usage") or {}
        out.update(ok=r.status_code == 200, model=b.get("model"),
                   chars=len(content) if isinstance(content, str) else 0,
                   out_tok=u.get("completion_tokens") or 0, cost=float(u.get("cost") or 0),
                   finish=ch.get("finish_reason"))
    except Exception as exc:
        out["exc"] = type(exc).__name__
    out["latency_s"] = round(time.perf_counter() - started, 1)
    return out


async def run():
    async with httpx.AsyncClient(timeout=200) as client:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def guarded(tier, model, n, p):
            async with sem:
                return await one(client, tier, model, n, p)

        tasks = [guarded(tier, model, n, p)
                 for tier, model in CANDIDATES.items()
                 for n, p in enumerate(tb.PROMPTS, 1)]
        rows = await asyncio.gather(*tasks)
    return rows


try:
    add_tiers()
    print(f"max_tokens={MAX_TOKENS}  candidates={CANDIDATES}")
    rows = asyncio.run(run())
    json.dump(rows, open("bakeoff_results.json", "w", encoding="utf-8"), indent=1)
finally:
    restore()
    same = open(CONFIG, encoding="utf-8").read() == original
    print(f"config restored byte-identical: {same}")

print(f"\n{'tier':<14}{'model':<30}{'ok':>5}{'empty':>7}{'med chars':>11}{'out tok':>9}{'cost $':>9}{'p50 s':>8}  finish")
for tier, model in CANDIDATES.items():
    rs = [r for r in rows if r["tier"] == tier]
    ok = sum(1 for r in rs if r["ok"])
    empty = sum(1 for r in rs if not r["chars"])
    chars = sorted(r["chars"] for r in rs)
    med = chars[len(chars) // 2] if chars else 0
    lat = sorted(r["latency_s"] or 0 for r in rs)
    from collections import Counter
    print(f"{tier:<14}{model:<30}{ok:>5}{empty:>7}{med:>11}{sum(r['out_tok'] for r in rs):>9}"
          f"{sum(r['cost'] for r in rs):>9.4f}{lat[len(lat)//2]:>8.1f}  {dict(Counter(r['finish'] for r in rs))}")

print("\nbaseline for comparison: v8 qwen3.8-max-0902 -> 15/15 ok, 0 empty, median 4936 chars")
print("                          v9 deepseek-v4.1-flash @2048 -> 14/15, 6 empty, median 1211 chars")
