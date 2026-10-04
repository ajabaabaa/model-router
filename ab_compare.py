"""Compare the stored v8 baseline (balanced = qwen3.8-max-0902) against the v9 rerun (current routing)."""

import json
import statistics
from collections import defaultdict
from pathlib import Path

text = Path("tier_benchmark_results.txt").read_text(encoding="utf-8", errors="replace")
dec = json.JSONDecoder()
rows, i = [], 0
while True:
    j = text.find("{", i)
    if j < 0:
        break
    try:
        v, k = dec.raw_decode(text[j:])
        if isinstance(v, dict):
            rows.append(v)
        i = j + k
    except Exception:
        i = j + 1

OLD = "tier-benchmark-v8-interactive-quality"
NEW = "tier-ab-v9-cheap-routing"


def stats(rs):
    lat = [r.get("latency_ms") or 0 for r in rs]
    chars = [r.get("content_character_count") or 0 for r in rs]
    return {
        "n": len(rs),
        "model": sorted({str(r.get("actual_model")) for r in rs}),
        "success": f"{sum(1 for r in rs if r.get('success'))}/{len(rs)}",
        "p50_s": round(statistics.median(lat) / 1000, 1) if lat else None,
        "max_s": round(max(lat) / 1000, 1) if lat else None,
        "out_tok": sum(r.get("output_tokens") or 0 for r in rs),
        "cost": round(sum(r.get("estimated_cost") or 0 for r in rs), 4),
        "median_chars": int(statistics.median(chars)) if chars else 0,
        "empty_answers": sum(1 for r in rs if not (r.get("message_content") or "").strip()),
        "finish": {f: sum(1 for r in rs if r.get("finish_reason") == f)
                   for f in {r.get("finish_reason") for r in rs}},
    }


for tier in ("balanced", "fast"):
    old = [r for r in rows if r.get("benchmark_run_id") == OLD and r.get("tier") == tier]
    new = [r for r in rows if r.get("benchmark_run_id") == NEW and r.get("tier") == tier]
    print(f"\n===== {tier.upper()} =====")
    print("  BEFORE (v8):", json.dumps(stats(old)) if old else "no rows")
    print("  AFTER  (v9):", json.dumps(stats(new)) if new else "NO ROWS - run did not complete")

newb = [r for r in rows if r.get("benchmark_run_id") == NEW and r.get("tier") == "balanced"]
if newb:
    nums = sorted({r.get("prompt_number") for r in newb})
    print(f"\nv9 balanced prompts completed: {len(nums)}/15 -> {nums}")
    print("\nv9 per-prompt detail (balanced):")
    for r in sorted(newb, key=lambda x: x.get("prompt_number") or 0):
        print(f"  p{r.get('prompt_number'):>2} {str(r.get('actual_model'))[:30]:<30} "
              f"ok={int(bool(r.get('success')))} {str(r.get('finish_reason')):<10} "
              f"chars={r.get('content_character_count') or 0:>6} out={(r.get('output_tokens') or 0):>5} "
              f"{round((r.get('latency_ms') or 0)/1000,1):>6}s ${r.get('estimated_cost') or 0:.5f} "
              f"exc={r.get('exception')}")
