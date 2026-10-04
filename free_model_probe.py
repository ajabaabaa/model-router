"""Careful OpenRouter probe: real tool support via supported_parameters, plus field inventory."""

import json
import os
import urllib.request

URL = "https://openrouter.ai/api/v1/models"
req = urllib.request.Request(URL, headers={"User-Agent": "cost-probe"})
key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
if key:
    req.add_header("Authorization", f"Bearer {key}")
data = json.load(urllib.request.urlopen(req, timeout=40))["data"]
by_id = {m["id"]: m for m in data}

print("=== fields present on a model object ===")
print(sorted(data[0].keys()))
print("\n=== sample supported_parameters ===")
print(data[0]["id"], "->", data[0].get("supported_parameters"))


def row(mid):
    m = by_id.get(mid)
    if not m:
        print(f"{mid:<44} NOT IN CATALOG")
        return
    sp = m.get("supported_parameters") or []
    p = m.get("pricing", {}) or {}
    pi, po = float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6
    cache = p.get("input_cache_read")
    cache_w = p.get("input_cache_write")
    print(
        f"{mid:<44} ctx={m.get('context_length', 0):>10,} tools={'Y' if 'tools' in sp else 'N'}"
        f" so={'Y' if 'structured_outputs' in sp else 'N'}"
        f" in=${pi:<7.3f} out=${po:<7.3f}"
        f" cache_read={float(cache) * 1e6 if cache is not None else -1:.3f}"
        f" cache_write={float(cache_w) * 1e6 if cache_w is not None else -1:.3f}"
    )


print("\n=== currently configured ===")
for mid in ["qwen/qwen3.8-max-0902", "qwen/qwen3.8-flash", "~z-ai/glm-flash-latest",
            "deepseek/deepseek-v4-flash", "minimax/minimax-m3", "minimax/minimax-m1"]:
    row(mid)

print("\n=== the model named in the request ===")
row("qwen/qwen3.7-max")
row("qwen/qwen3.7-flash")
row("qwen/qwen3.7-plus")

print("\n=== ALL free routes, with real tool support ===")
free = [
    m for m in data
    if float((m.get("pricing") or {}).get("prompt", 1)) == 0
    and float((m.get("pricing") or {}).get("completion", 1)) == 0
]
free.sort(key=lambda m: (
    "tools" not in (m.get("supported_parameters") or []),
    -(m.get("context_length") or 0),
))
print(f"{'id':<44}{'ctx':>10}{'tools':>6}{'so':>4}  name")
for m in free:
    sp = m.get("supported_parameters") or []
    print(
        f"{m['id']:<44}{m.get('context_length', 0):>10,}"
        f"{'Y' if 'tools' in sp else 'N':>6}{'Y' if 'structured_outputs' in sp else 'N':>4}  "
        f"{(m.get('name') or '')[:34]}"
    )
print(f"\nfree routes WITH tool support: "
      f"{sum(1 for m in free if 'tools' in (m.get('supported_parameters') or []))} / {len(free)}")

print("\n=== cheapest paid input routes overall (in <= $0.10/1M) ===")
cheap = []
for m in data:
    p = m.get("pricing") or {}
    pi, po = float(p.get("prompt", 1e9)) * 1e6, float(p.get("completion", 1e9)) * 1e6
    if 0 < pi <= 0.10:
        cheap.append((pi, po, m))
cheap.sort(key=lambda x: (x[0], x[1], x[2]["id"]))
print(f"{'id':<44}{'ctx':>10}{'tools':>6}{'in':>8}{'out':>8}  cache_read")
for pi, po, m in cheap:
    sp = m.get("supported_parameters") or []
    c = (m.get("pricing") or {}).get("input_cache_read")
    print(
        f"{m['id']:<44}{m.get('context_length', 0):>10,}{'Y' if 'tools' in sp else 'N':>6}"
        f"{pi:>8.3f}{po:>8.3f}  {float(c) * 1e6 if c is not None else -1:.3f}"
    )
