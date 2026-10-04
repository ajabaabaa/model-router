"""Hourly cost watch for openclaw-router. Read-only; prints one compact report + alerts.

Run:  .venv\\Scripts\\python.exe cost_watch.py [hours]   (default 1)
Exit 0 = OK, 1 = at least one ALERT line emitted (so a scheduler can key off it).
"""

import json
import sqlite3
import sys

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
DB = "router_telemetry.db"
CONFIG = "router_config.json"

# A served model outside the configured chains means something drifted (a fallback list
# edited by hand, a tier pointed elsewhere). Cheaper to detect than to discover in a bill.
cfg = json.loads(open(CONFIG, encoding="utf-8").read())
DECLARED = set()
for tier in cfg["tiers"].values():
    DECLARED.add(tier["primary"])
    DECLARED.update(tier.get("fallbacks", []))

alerts = []
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
# Telemetry stores "YYYY-MM-DDTHH:MM:SSZ" but datetime('now') yields "YYYY-MM-DD HH:MM:SS".
# Comparing those as strings makes every bound match (0x54 'T' > 0x20 ' '), silently widening
# "last 1 hour" to "all of today". Build the bound in the stored format instead.
BOUND = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
W = f"timestamp >= {BOUND}"
args = (f"-{HOURS:g} hours",)


def window(modifier):
    return conn.execute(f"select count(*), sum(estimated_cost) from telemetry where timestamp >= "
                        f"strftime('%Y-%m-%dT%H:%M:%SZ','now',?)", (modifier,)).fetchone()

row = conn.execute(
    f"select count(*) c, sum(estimated_cost) cost, sum(input_tokens) tin, sum(output_tokens) tout, "
    f"sum(case when success=0 then 1 else 0 end) fails, "
    f"sum(case when fallback_count>0 then 1 else 0 end) fbs "
    f"from telemetry where {W}",
    args,
).fetchone()
reqs, cost, tin, tout, fails, fbs = (row[0] or 0), (row[0] and row[1] or 0.0), row[2] or 0, row[3] or 0, row[4] or 0, row[5] or 0

print(f"COST WATCH  last {HOURS:g}h   ({conn.execute('select max(timestamp) from telemetry').fetchone()[0]})")
print("-" * 78)
print(f"requests      {reqs:>7}")
print(f"spend         ${cost:>7.4f}")
print(f"tokens        in {tin:,}  out {tout:,}")
if tin:
    print(f"effective     ${cost / (tin / 1e6):.4f} per 1M input-equivalent")
if reqs:
    week_actual = window("-7 days")[1] or 0.0
    # Project from the freshest 20 minutes, not the whole window: right after a routing change
    # the window is full of old-rate rows and the naive projection reads ~10x too high.
    short_n, short_cost = window("-20 minutes")
    short_cost = short_cost or 0.0
    if short_n:
        print(
            f"run-rate      ${short_cost / short_n:.5f}/req over last {short_n} reqs -> "
            f"projected ${short_cost * 3 * 24 * 7:.2f}/week at this pace"
        )
    print(f"window        ${cost:.4f} spend / {reqs} reqs; last 7 days actual ${week_actual:.2f}")
print(f"failures      {fails:>7}    fallback used {fbs:>6}"
      f"{f'  ({100.0 * fbs / reqs:.1f}% of requests)' if reqs else ''}")

print("\nper agent (this window)")
for a, n, c, t in conn.execute(
    f"select coalesce(agent,'(untagged)') ag, count(*) c, sum(estimated_cost) cost, sum(input_tokens) t "
    f"from telemetry where {W} group by ag order by cost desc limit 8",
    args,
):
    print(f"  {a:<20} reqs={n:>5}  in={t or 0:>12,}  ${c or 0:.4f}")

print("\nper model served (this window)")
# A model outside the config is only actionable if it is being served NOW. Right after a
# routing change the window still holds the old rows, and alerting on those trains everyone
# to ignore the alert. Age is measured with SQLite's clock, because the rows are written with
# time.gmtime() and compared against datetime('now') - Python's clock can disagree with both.
for m, n, c, last, age_s in conn.execute(
    f"select actual_model m, count(*) c, sum(estimated_cost) cost, max(timestamp) last, "
    f"strftime('%s','now') - strftime('%s', replace(replace(max(timestamp),'T',' '),'Z','')) age "
    f"from telemetry where {W} group by m order by cost desc",
    args,
):
    ago = age_s / 60.0 if age_s is not None else None
    age = f"last {ago:.0f}m ago" if ago is not None else "last ?"
    if m and m not in DECLARED:
        if ago is not None and ago <= 15:
            print(f"  {m:<38} reqs={n:>5}  ${c or 0:.4f}  {age}  <-- OFF-CONFIG, LIVE NOW")
            alerts.append(f"{m} served {n} requests in-window and is not declared in {CONFIG} "
                          f"(still being served {ago:.0f} min ago)")
        else:
            print(f"  {m:<38} reqs={n:>5}  ${c or 0:.4f}  {age}  (off-config but stale - pre-change rows)")
    else:
        print(f"  {str(m):<38} reqs={n:>5}  ${c or 0:.4f}  {age}")

# 429 storm on a free primary is expected to some degree; a storm means the paid floor
# is carrying the load and latency is paying for it.
rate429 = conn.execute(
    f"select count(*) from telemetry where {W} and attempt_outcomes like '%429%'", args
).fetchone()[0]
if reqs and rate429 and 100.0 * rate429 / reqs > 25:
    alerts.append(f"{rate429}/{reqs} requests ({100.0 * rate429 / reqs:.0f}%) hit a 429 upstream")

big = conn.execute(
    f"select timestamp, agent, selected_tier, actual_model, input_tokens, estimated_cost "
    f"from telemetry where {W} and estimated_cost > 0.25 order by estimated_cost desc limit 5",
    args,
).fetchall()
for ts, a, tier, m, tin_, c in big:
    print(f"  NOTE expensive turn: {ts} agent={a} tier={tier} {m} in={tin_:,} ${c:.4f}")
if len(big) >= 3:
    alerts.append(f"{len(big)} turns over $0.25 in {HOURS:g}h - context size, not tier, is driving cost")

fails_rate = 100.0 * fails / reqs if reqs else 0.0
if fails_rate > 5 and reqs > 20:
    alerts.append(f"{fails}/{reqs} requests failed ({fails_rate:.1f}%)")

print("-" * 78)
if alerts:
    for a in alerts:
        print(f"ALERT: {a}")
else:
    print("no alerts")
conn.close()
sys.exit(1 if alerts else 0)
