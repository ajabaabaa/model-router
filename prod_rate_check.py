"""Production per-request token rates: old primary vs current primary, same tier."""

import sqlite3

conn = sqlite3.connect("file:router_telemetry.db?mode=ro", uri=True)
# The routing cutover was the 06:2xZ restart; 10:20Z was a LATER restart and would have
# excluded the financebot burst, silently shrinking the "after" sample to 15 rows.
CUT = "2026-10-04T06:20:00Z"

print("=== balanced tier, real agent traffic (excluding benchmark/verify rows) ===")
for label, where, args in [
    ("BEFORE (qwen3.8-max-0902)", "actual_model='qwen/qwen3.8-max-0902' and timestamp>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')", ()),
    ("AFTER  (deepseek-v4.1-flash)", f"actual_model='deepseek/deepseek-v4.1-flash' and timestamp>='{CUT}'", ()),
]:
    n, tin, tout, cost = conn.execute(
        f"select count(*), sum(input_tokens), sum(output_tokens), sum(estimated_cost) from telemetry "
        f"where selected_tier='balanced' and {where} and coalesce(agent,'') not in "
        f"('verify','smoketest-coordinator','')", args
    ).fetchone()
    if not n:
        print(f"  {label:<28} no rows")
        continue
    print(f"  {label:<28} reqs={n:>5}  in/req={tin / n:>10,.0f}  out/req={tout / n:>8,.0f}  "
          f"$/req=${cost / n:.6f}  total=${cost:.4f}")

print("\n=== weekly projection from measured per-request rates, at last week's volume ===")
week_reqs, week_in, week_out = conn.execute(
    "select count(*), sum(input_tokens), sum(output_tokens) from telemetry "
    "where selected_tier='balanced' and timestamp>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days') "
    "and coalesce(agent,'') not in ('verify','smoketest-coordinator','')"
).fetchone()
print(f"  last 7d balanced agent traffic: {week_reqs:,} reqs, {week_in / 1e6:.1f}M in, {week_out / 1e6:.2f}M out")
for model, pin, pout in [
    ("qwen3.8-max-0902 (old)", 2.00, 6.00),
    ("deepseek-v4.1-flash (now)", 0.003, 2.40),
    ("glm-flash-latest (candidate)", 0.035, 0.50),
    ("minimax-m3 (candidate)", 0.30, 1.20),
]:
    print(f"  {model:<30} ${week_in / 1e6 * pin:>7.2f} in + ${week_out / 1e6 * pout:>6.2f} out = "
          f"${week_in / 1e6 * pin + week_out / 1e6 * pout:>7.2f}/wk at list prices")

print("\n=== if v4.1-flash inflates output tokens 3x (bake-off ratio), at glm's token count ===")
inflated = week_out * 3.28
print(f"  v4.1-flash with inflated output: {inflated / 1e6:.1f}M out -> ${week_in / 1e6 * 0.003 + inflated / 1e6 * 2.40:.2f}/wk")
print(f"  glm at baseline output:          ${week_in / 1e6 * 0.035 + week_out / 1e6 * 0.50:.2f}/wk")

print("\n=== latency by model on balanced, real traffic (p50/p95 ms) ===")
for m in ("qwen/qwen3.8-max-0902", "deepseek/deepseek-v4.1-flash"):
    lat = [r[0] for r in conn.execute(
        f"select latency_ms from telemetry where selected_tier='balanced' and actual_model=? "
        f"and latency_ms is not null order by id desc limit 500", (m,))]
    if len(lat) < 5:
        print(f"  {m:<32} too few rows ({len(lat)})")
        continue
    lat.sort()
    print(f"  {m:<32} n={len(lat):>4} p50={lat[len(lat)//2]:>7,.0f}ms  p95={lat[int(len(lat)*.95)]:>7,.0f}ms")
conn.close()
