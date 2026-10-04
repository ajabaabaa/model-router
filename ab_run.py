"""Run the existing 15-prompt benchmark against the CURRENT routing, under a new run id.

Reuses tier_benchmark's harness verbatim (same prompts, same payload shape, same budgets) and
only swaps the run id, so the new rows are directly comparable with the stored v8 baseline
whose balanced tier was qwen3.8-max-0902.
"""

import asyncio
import json

import tier_benchmark as tb

NEW_RUN = "tier-ab-v9-cheap-routing"

print(json.dumps({
    "baseline_run": tb.RUN_ID,
    "new_run": NEW_RUN,
    "baseline_balanced_model": "qwen/qwen3.8-max-0902 (stored v8)",
    "current_balanced_model": json.load(open("router_config.json", encoding="utf-8"))["tiers"]["balanced"]["primary"],
    "prompts": len(tb.PROMPTS),
    "tiers": ["balanced", "fast"],
}))

tb.RUN_ID = NEW_RUN
for tier in ("balanced", "fast"):
    asyncio.run(tb.main(tier))
