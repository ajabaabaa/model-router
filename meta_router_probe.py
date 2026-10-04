"""List OpenRouter-native meta routes (openrouter/*) with real capability fields."""

import json
import os
import urllib.request

URL = "https://openrouter.ai/api/v1/models"
req = urllib.request.Request(URL, headers={"User-Agent": "router-probe"})
key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
if key:
    req.add_header("Authorization", f"Bearer {key}")
data = json.load(urllib.request.urlopen(req, timeout=40))["data"]

meta = [m for m in data if m["id"].startswith("openrouter/")]
print(f"openrouter/* routes: {len(meta)}\n")
for m in sorted(meta, key=lambda x: x["id"]):
    p = m.get("pricing") or {}
    sp = m.get("supported_parameters") or []
    print(f"\n{m['id']}  ({m.get('name')})")
    print(
        f"  ctx={m.get('context_length')}  in=${float(p.get('prompt', 0)) * 1e6:.3f}/1M"
        f"  out=${float(p.get('completion', 0)) * 1e6:.3f}/1M"
    )
    print(f"  pricing keys: {sorted(p.keys())}")
    print(f"  tools={'Y' if 'tools' in sp else 'N'}  routing-relevant params: {sp}")
    print(f"  per_request_limits={m.get('per_request_limits')}")
    desc = (m.get("description") or "").replace("\n", " ")
    print(f"  desc: {desc[:400]}")

print("\n\n=== any route mentioning auto/ensemble/router in id ===")
for m in data:
    if any(w in m["id"] for w in ("auto", "ensemble", "router", "free")):
        p = m.get("pricing") or {}
        print(
            f"{m['id']:<46} ctx={str(m.get('context_length')):>9} "
            f"in=${float(p.get('prompt', 0)) * 1e6:.3f} out=${float(p.get('completion', 0)) * 1e6:.3f}"
            f"  {m.get('name','')[:30]}"
        )
