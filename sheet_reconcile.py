"""Reconcile the shared sheet against the live OpenRouter catalog (zero-cost: catalog only)."""

import csv
import json
import os
import re
import urllib.request
import zipfile

SID = "1FTTfPz5g-2Rwwdanqz7aIGccgndba1rHB4X4RZzVmcE"
GID = "1364289760"

print("=" * 100)
print("TAB INVENTORY (from xlsx workbook.xml)")
print("=" * 100)
try:
    with zipfile.ZipFile("model_list.xlsx") as z:
        wb = z.read("xl/workbook.xml").decode("utf-8", errors="replace")
    for m in re.finditer(r'<sheet[^>]*name="([^"]+)"[^>]*sheetId="([^"]+)"', wb):
        print(f"  tab: {m.group(1):<32} sheetId={m.group(2)}")
    for m in re.finditer(r"<sheet[^>]*>", wb):
        pass
except Exception as e:
    print(f"  xlsx parse failed: {type(e).__name__}: {e}")

rows = list(csv.DictReader(open("model_list.csv", encoding="utf-8", newline="")))
print(f"\nrows in tab gid={GID} = {len(rows)}")
print(f"columns: {list(rows[0].keys())}")

tiers = {}
for r in rows:
    tiers.setdefault(r.get("Pricing Tier") or "(blank)", []).append(r)
print("\n=== pricing tiers present in this tab ===")
for t, items in tiers.items():
    print(f"  {t:<36} {len(items):>3} models")

URL = "https://openrouter.ai/api/v1/models"
req = urllib.request.Request(URL, headers={"User-Agent": "reconcile"})
key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
if key:
    req.add_header("Authorization", f"Bearer {key}")
cat = {m["id"]: m for m in json.load(urllib.request.urlopen(req, timeout=40))["data"]}


def money(s):
    m = re.search(r"[\d.]+", str(s or ""))
    return float(m.group()) if m else None


def num(s):
    m = re.search(r"[\d,]+", str(s or ""))
    return int(m.group().replace(",", "")) if m else None


print("\n" + "=" * 100)
print("SHEET CLAIM vs LIVE OPENROUTER  (this tab)")
print("=" * 100)
print(f"{'model slug':<40}{'sheet in/out':>16}{'live in/out':>16}{'sheet ctx':>11}{'live ctx':>11}{'tools':>6}  status")
missing, stale, ok = [], [], []
for r in rows:
    slug = (r.get("Model Slug (API Endpoint)") or "").strip()
    if not slug:
        continue
    live = cat.get(slug)
    si, so = money(r.get("Input Cost ($/1M)")), money(r.get("Output Cost ($/1M)"))
    sctx = num(r.get("Context Window"))
    if not live:
        print(f"{slug:<40}{f'${si}/${so}':>16}{'-':>16}{str(sctx):>11}{'-':>11}{'-':>6}  NOT IN LIVE CATALOG")
        missing.append(slug)
        continue
    p = live.get("pricing") or {}
    li, lo = float(p.get("prompt", 0)) * 1e6, float(p.get("completion", 0)) * 1e6
    lctx = live.get("context_length")
    tools = "Y" if "tools" in (live.get("supported_parameters") or []) else "N"
    drift = ""
    if si is not None and abs(li - si) > 0.01:
        drift = "PRICE-DRIFT"
    if sctx and lctx and abs(lctx - sctx) / max(sctx, 1) > 0.05:
        drift = (drift + " CTX-DRIFT").strip()
    tag = drift or "match"
    (ok if tag == "match" else stale).append(slug)
    print(
        f"{slug:<40}{f'${si}/${so}':>16}{f'${li:g}/${lo:g}':>16}{str(sctx):>11}{str(lctx):>11}{tools:>6}  {tag}"
    )

print(f"\nmatch={len(ok)}  drift={len(stale)}  not-in-catalog={len(missing)}")
if missing:
    print("NOT IN LIVE CATALOG (sheet names these, OpenRouter does not serve them):")
    for s in missing:
        print(f"  - {s}")
if stale:
    print("DRIFT (sheet price/ctx disagrees with live):")
    for s in stale:
        print(f"  - {s}")

print("\n" + "=" * 100)
print("CHEAPEST ROUTABLE OPTIONS, live prices, tools + >=200k ctx")
print("=" * 100)
cands = []
for mid, m in cat.items():
    p = m.get("pricing") or {}
    pi, po = float(p.get("prompt", 1e9)) * 1e6, float(p.get("completion", 1e9)) * 1e6
    sp = m.get("supported_parameters") or []
    if "tools" not in sp:
        continue
    if (m.get("context_length") or 0) < 200000:
        continue
    cands.append((pi, po, mid))
cands.sort(key=lambda x: (x[0], x[1], x[2]))
print(f"{'id':<46}{'in':>9}{'out':>9}{'ctx':>11}  cache_read")
for pi, po, mid in cands[:25]:
    p = cat[mid].get("pricing") or {}
    cr = p.get("input_cache_read")
    print(
        f"{mid:<46}{pi:>9.3f}{po:>9.3f}{cat[mid].get('context_length', 0):>11,}"
        f"  {float(cr) * 1e6 if cr is not None else -1:.3f}"
    )
