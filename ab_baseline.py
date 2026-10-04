"""Summarise stored benchmark rows so the A/B baseline is known, not assumed."""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

text = Path("tier_benchmark_results.txt").read_text(encoding="utf-8", errors="replace")
dec = json.JSONDecoder()
rows = []
i = 0
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

RUN = "tier-benchmark-v8-interactive-quality"
v8 = [r for r in rows if r.get("benchmark_run_id") == RUN]
print(f"total rows in file: {len(rows)}   v8 rows: {len(v8)}")

groups = defaultdict(list)
for r in v8:
    groups[(r.get("tier"), r.get("actual_model"))].append(r)

print(f"\n{'tier':<10}{'model':<34}{'n':>4}{'succ':>6}{'p50 ms':>10}{'max ms':>10}{'out tok':>9}{'cost $':>10}{'content chars':>14}")
for (tier, model), rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
    lat = [r.get("latency_ms") or 0 for r in rs]
    succ = sum(1 for r in rs if r.get("success")) / len(rs)
    chars = [r.get("content_character_count") or 0 for r in rs]
    print(f"{str(tier):<10}{str(model):<34}{len(rs):>4}{succ:>6.2f}{statistics.median(lat):>10.0f}"
          f"{max(lat):>10.0f}{sum(r.get('output_tokens') or 0 for r in rs):>9}"
          f"{sum(r.get('estimated_cost') or 0 for r in rs):>10.4f}{statistics.median(chars):>14.0f}")

print("\n=== v8 prompt coverage by tier ===")
for tier in ("fast", "balanced"):
    nums = sorted({r.get("prompt_number") for r in v8 if r.get("tier") == tier})
    print(f"  {tier:<9} {len(nums)} prompts: {nums}")

empty = [(r.get("tier"), r.get("prompt_number")) for r in v8
         if not (r.get("message_content") or "").strip()]
print(f"\nv8 rows with EMPTY final content: {len(empty)} {empty[:6]}")

print("\n=== other run ids present (for context) ===")
print(Counter(r.get("benchmark_run_id") or "(legacy)" for r in rows).most_common(6))
