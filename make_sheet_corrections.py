"""Build a paste-ready correction of the model-tracking sheet.

Only rows I actually probed get a measured verdict; everything else is marked NOT PROBED rather
than guessed, because the sheet's existing ZDR column is asserted and demonstrably wrong.
"""

import csv
import json
import os
import urllib.request

KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                             headers={"User-Agent": "sheet-fix", "Authorization": f"Bearer {KEY}"})
cat = {m["id"]: m for m in json.load(urllib.request.urlopen(req, timeout=40))["data"]}

# Measured this session against /chat/completions with a tiny prompt per gate.
MEASURED = {
    "deepseek/deepseek-v4.1-flash": ("PASS 200", "PASS 200", "tool call OK (zdr+deny)"),
    "deepseek/deepseek-v4-flash": ("PASS 200", "PASS 200", "tool call OK (zdr+deny)"),
    "~z-ai/glm-flash-latest": ("PASS 200", "PASS 200", "tool call OK (zdr+deny)"),
    "minimax/minimax-m3": ("PASS 200", "PASS 200", "tool call OK (zdr+deny)"),
    "qwen/qwen3.7-max": ("404 no endpoint", "PASS 200", "tool call OK only with max_tokens>=4000"),
    "qwen/qwen3.8-max-0902": ("404 no endpoint", "PASS 200", "not probed for tools"),
    "qwen/qwen3.8-flash": ("404 no endpoint", "404 no endpoint", "not probed"),
    "qwen/qwen3.7-flash": ("404 no endpoint", "404 no endpoint", "no tool call at 48 tok (budget, not capability)"),
    "qwen/qwen3.8-27b:free": ("PASS 200", "PASS 200", "tool call OK; 429 seen in production"),
    "inclusionai/ling-3.1-flash": ("PASS 200", "PASS 200", "tool call OK"),
    "openrouter/free": ("PASS 200 (random pick)", "PASS 200 (random pick)",
                        "unreliable: no tool call under no-gate, OK under zdr"),
    "nvidia/nemotron-3-ultra-550b-a55b:free": ("404 no endpoint", "404 blocked", "no tool call"),
    "google/gemma-4-31b-it:free": ("not probed", "not probed", "429 in production"),
    "thinkingmachines/inkling:free": ("403 plan-gated", "403 plan-gated", "unusable from this key"),
    "poolside/laguna-s-2.1:free": ("404 blocked", "404 blocked", "finish=error"),
    "stealth/space-bunny-alpha": ("404 no endpoint", "PASS 200", "not probed for tools"),
    "anthropic/claude-opus-5.5": ("400 on forced tool_choice", "400 on forced tool_choice",
                                  "works with tool_choice auto"),
    "typesafe/jev-router": ("not probed", "not probed", "not probed"),
}

rows = list(csv.DictReader(open("model_list.csv", encoding="utf-8", errors="replace")))


def money(value):
    """Sheet prices arrive as '$15.00', '0.08', 'Free' or '' - parse what is numeric."""
    import re
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else 0.0


out = []
for r in rows:
    slug = (r.get("Model Slug (API Endpoint)") or "").strip()
    if not slug:
        continue
    live = cat.get(slug)
    if live:
        p = live.get("pricing") or {}
        sp = live.get("supported_parameters") or []
        zdr, deny, tools = MEASURED.get(slug, ("NOT PROBED", "NOT PROBED", "NOT PROBED"))
        verdict = ("OK" if live.get("context_length", 0) >= 200000 and "tools" in sp
                   else "CHECK ctx/tools")
        out.append({
            "model_slug": slug,
            "exists_on_openrouter": "yes",
            "sheet_input": r.get("Input Cost ($/1M)", ""),
            "live_input": f"{float(p.get('prompt', 0)) * 1e6:.4g}",
            "sheet_output": r.get("Output Cost ($/1M)", ""),
            "live_output": f"{float(p.get('completion', 0)) * 1e6:.4g}",
            "sheet_context": r.get("Context Window", ""),
            "live_context": live.get("context_length"),
            "live_supports_tools": "yes" if "tools" in sp else "no",
            "sheet_zdr_claim": r.get("Zero Data Retention (ZDR) Eligible?", ""),
            "measured_zdr_true": zdr,
            "measured_data_collection_deny": deny,
            "measured_tool_calls": tools,
            "price_drift": ("yes" if abs(float(p.get("prompt", 0)) * 1e6 - money(r.get("Input Cost ($/1M)"))) > 0.01
                            else "no"),
            "routing_verdict": verdict,
        })
    else:
        out.append({
            "model_slug": slug, "exists_on_openrouter": "NO - cannot route",
            "sheet_input": r.get("Input Cost ($/1M)", ""), "live_input": "-",
            "sheet_output": r.get("Output Cost ($/1M)", ""), "live_output": "-",
            "sheet_context": r.get("Context Window", ""), "live_context": "-",
            "live_supports_tools": "-",
            "sheet_zdr_claim": r.get("Zero Data Retention (ZDR) Eligible?", ""),
            "measured_zdr_true": "n/a", "measured_data_collection_deny": "n/a",
            "measured_tool_calls": "n/a", "price_drift": "n/a",
            "routing_verdict": "REMOVE from sheet or fix the slug",
        })

# Models the router actually uses that the sheet omits entirely.
have = {o["model_slug"] for o in out}
cfg = json.load(open("router_config.json", encoding="utf-8"))["tiers"]
missing = []
for name, tier in cfg.items():
    for slug in [tier["primary"], *tier.get("fallbacks", [])]:
        if slug not in have:
            live = cat.get(slug) or {}
            p = live.get("pricing") or {}
            missing.append({
                "model_slug": slug, "exists_on_openrouter": "yes" if live else "NO",
                "sheet_input": "(absent from sheet)",
                "live_input": f"{float(p.get('prompt', 0)) * 1e6:.4g}" if live else "-",
                "sheet_output": "(absent from sheet)",
                "live_output": f"{float(p.get('completion', 0)) * 1e6:.4g}" if live else "-",
                "sheet_context": "-", "live_context": live.get("context_length", "-"),
                "live_supports_tools": "yes" if "tools" in (live.get("supported_parameters") or []) else "no",
                "sheet_zdr_claim": "(absent)",
                "measured_zdr_true": MEASURED.get(slug, ("NOT PROBED",))[0],
                "measured_data_collection_deny": MEASURED.get(slug, ("", "NOT PROBED"))[1],
                "measured_tool_calls": MEASURED.get(slug, ("", "", "NOT PROBED"))[2],
                "price_drift": "-", "routing_verdict": f"IN USE as {name} - add to sheet",
            })

fields = list(out[0].keys())
with open("sheet_corrections.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=fields)
    w.writeheader()
    w.writerows(out + missing)

bad = [o for o in out if o["exists_on_openrouter"] != "yes"]
drift = [o for o in out if o["price_drift"] == "yes"]
wrong_zdr = [o for o in out if o["measured_zdr_true"].startswith("404")
             and "yes" in (o["sheet_zdr_claim"] or "").lower()]
print(f"wrote sheet_corrections.csv: {len(out)} sheet rows + {len(missing)} in-use rows missing from sheet")
print(f"  cannot route (slug absent on OpenRouter): {len(bad)}")
print(f"  price drift vs sheet:                    {len(drift)}")
print(f"  sheet claims ZDR but zdr:true 404s:      {len(wrong_zdr)}")
for o in wrong_zdr[:10]:
    print(f"     - {o['model_slug']}")
for o in bad[:20]:
    print(f"     x {o['model_slug']}")
