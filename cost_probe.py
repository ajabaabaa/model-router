"""Read-only cost probe: where the money actually went, grouped by model/tier/agent."""

import sqlite3
import sys

DB = "router_telemetry.db"
DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

n = conn.execute("select count(*) c from telemetry").fetchone()["c"]
lo, hi = conn.execute("select min(timestamp), max(timestamp) from telemetry").fetchone()
print(f"all-time rows={n}  span={lo} .. {hi}")

# Bound must be built in the stored format: comparing "YYYY-MM-DDTHH:MM:SSZ" against
# datetime('now')'s space-separated form matches everything (0x54 'T' > 0x20 ' ').
where = "timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
args = (f"-{int(DAYS)} days",)

tot = conn.execute(
    f"select count(*) c, sum(estimated_cost) cost, sum(input_tokens) it, sum(output_tokens) ot, "
    f"sum(case when success then 0 else 1 end) fails, "
    f"sum(case when fallback_count>0 then 1 else 0 end) fbs "
    f"from telemetry where {where}",
    args,
).fetchone()
print(
    f"\n=== last {DAYS:g} days ===\n"
    f"requests={tot['c']}  cost=${(tot['cost'] or 0):.4f}  "
    f"in={(tot['it'] or 0):,}  out={(tot['ot'] or 0):,}  "
    f"failed={tot['fails']}  used-fallback={tot['fbs']}"
)
print(f"avg $/request = {(tot['cost'] or 0) / tot['c']:.4f}" if tot["c"] else "")


def group(label, keys):
    q = (
        f"select {keys} k, count(*) c, sum(estimated_cost) cost, sum(input_tokens) it, "
        f"sum(output_tokens) ot, avg(latency_ms) lat, "
        f"sum(case when fallback_count>0 then 1 else 0 end) fb "
        f"from telemetry where {where} group by {keys} order by cost desc"
    )
    print(f"\n--- by {label} ---")
    print(f"{'key':<40}{'req':>6}{'cost':>10}{'in':>13}{'out':>10}{'$/req':>9}{'in:out':>8}{'fb':>5}")
    for r in conn.execute(q, args):
        cost = r["cost"] or 0.0
        per = cost / r["c"] if r["c"] else 0.0
        it, ot = r["it"] or 0, r["ot"] or 0
        ratio = f"{it / ot:.0f}:1" if ot else "-"
        print(
            f"{str(r['k'])[:39]:<40}{r['c']:>6}{cost:>10.4f}{it:>13,}{ot:>10,}"
            f"{per:>9.4f}{ratio:>8}{r['fb']:>5}"
        )


group("actual model", "actual_model")
group("selected tier", "selected_tier")
group("agent", "coalesce(agent,'(untagged)')")
group("requested tier", "requested_tier")
group("task type", "coalesce(task_type,'(none)')")

print("\n=== daily ===")
for r in conn.execute(
    f"select date(timestamp) d, count(*) c, sum(estimated_cost) cost, sum(input_tokens) it "
    f"from telemetry where {where} group by date(timestamp) order by d",
    args,
):
    print(f"{r['d']}  req={r['c']:>5}  cost=${(r['cost'] or 0):>8.4f}  in={r['it'] or 0:>12,}")

print("\n=== top 15 most expensive single requests ===")
for r in conn.execute(
    f"select timestamp, agent, selected_tier, actual_model, input_tokens, output_tokens, "
    f"estimated_cost, request_has_tools, finish_reason from telemetry where {where} "
    f"order by estimated_cost desc limit 15",
    args,
):
    print(
        f"{r['timestamp']}  {str(r['agent'])[:12]:<12} {str(r['selected_tier']):<9} "
        f"{str(r['actual_model'])[:30]:<30} in={r['input_tokens']:>9,} out={r['output_tokens']:>7,} "
        f"${r['estimated_cost']:>7.4f} tools={r['request_has_tools']} fr={r['finish_reason']}"
    )

print("\n=== implied blended $/1M input tokens, per model ===")
for r in conn.execute(
    f"select actual_model m, sum(estimated_cost) cost, sum(input_tokens) it, sum(output_tokens) ot "
    f"from telemetry where {where} group by actual_model having cost > 0 order by cost desc",
    args,
):
    print(
        f"{str(r['m'])[:36]:<36} cost=${(r['cost'] or 0):>9.4f} in={r['it'] or 0:>12,} out={r['ot'] or 0:>10,}"
    )

conn.close()
