"""Hourly cost watch for openclaw-router. Read-only; prints one compact report + alerts.

Run:  .venv\\Scripts\\python.exe cost_watch.py [hours]   (default 1)
Exit 0 = OK, 1 = at least one ALERT line emitted (so a scheduler can key off it).
"""

import json
import os
import sqlite3
import sys
import urllib.request

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

# OpenHuman and OpenClaw have no usable failover if this process dies - their fallback chains
# resolve to other tiers on this same server, and OpenHuman's reliability.model_fallbacks is
# dead config in 0.64.10. Liveness is therefore part of the cost watch, not a separate concern.
# Overridable so the alert path can be exercised for real (point it at a dead port) instead of
# trusting a code path that only ever runs when something is already wrong.
PROBE_BASE = os.environ.get("COST_WATCH_PROBE_BASE", "http://127.0.0.1:6060").rstrip("/")
for probe, label in ((PROBE_BASE + "/health", "/health"),
                     (PROBE_BASE + "/v1/models", "/v1/models")):
    try:
        with urllib.request.urlopen(probe, timeout=15) as resp:
            if resp.status != 200:
                alerts.append(f"router {label} returned HTTP {resp.status}")
    except Exception as exc:
        alerts.append(f"router {label} unreachable: {type(exc).__name__} - agents have no failover")
# Requests that carry neither a tag nor a prefix are attributed to "openhuman" by the router, so
# any ad-hoc script that posts a bare tier name lands in that bucket too. Benchmark traffic is
# long-form generation and dwarfs real agent turns - projecting from it read $156/wk on an hour
# where the agents spent pennies. Keep it out of the run-rate and say so.
SYNTHETIC = ("benchmark", "verify", "verify2", "bakeoff", "cand", "smoketest", "router-test", "ab-")
# Traffic on a tier that no longer exists in the config is by definition not production - this
# catches bake-off rows written before the benchmark tag existed, whose agent column is polluted
# with the inferred "openhuman" label and so cannot be filtered by name.
_declared = ", ".join("'" + t + "'" for t in cfg["tiers"]) or "''"
SYN_FILTER = " and ".join(["coalesce(agent,'') NOT LIKE ?"] * len(SYNTHETIC)
                          + [f"selected_tier IN ({_declared})"])
SYN_ARGS = tuple(f"%{s}%" for s in SYNTHETIC)
# Telemetry stores "YYYY-MM-DDTHH:MM:SSZ" but datetime('now') yields "YYYY-MM-DD HH:MM:SS".
# Comparing those as strings makes every bound match (0x54 'T' > 0x20 ' '), silently widening
# "last 1 hour" to "all of today". Build the bound in the stored format instead.
BOUND = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
W = f"timestamp >= {BOUND}"
args = (f"-{HOURS:g} hours",)


def window(modifier, real_only=False):
    sql = (f"select count(*), sum(estimated_cost) from telemetry where timestamp >= "
           f"strftime('%Y-%m-%dT%H:%M:%SZ','now',?)")
    args = [modifier]
    if real_only:
        sql += f" and {SYN_FILTER}"
        args.extend(SYN_ARGS)
    return conn.execute(sql, tuple(args)).fetchone()

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
    # Deliberately NOT cost/input-tokens: after the routing change output dominates (one window
    # was 983 in vs 19,890 out), so that ratio reads like a frontier model price and means nothing.
    print(f"per request   ${cost / reqs:.5f}" if reqs else "")
if reqs:
    week_actual = window("-7 days", real_only=True)[1] or 0.0
    # Project from the freshest 20 minutes of *agent* traffic only. A benchmark run inside the
    # window is long-form generation and inflates the rate by an order of magnitude.
    short_n, short_cost = window("-20 minutes", real_only=True)
    short_cost = short_cost or 0.0
    if short_n:
        print(
            f"run-rate      ${short_cost / short_n:.5f}/req over {short_n} agent reqs (20m) -> "
            f"projected ${short_cost * 3 * 24 * 7:.2f}/week at this pace"
        )
    else:
        print("run-rate      no agent traffic in the last 20 min (benchmark rows excluded)")
    syn_n, syn_cost = conn.execute(
        f"select count(*), sum(estimated_cost) from telemetry where {W} and not ({SYN_FILTER})",
        (args[0],) + SYN_ARGS,
    ).fetchone()
    if syn_n:
        print(f"synthetic     {syn_n} benchmark/probe reqs = ${syn_cost or 0:.4f} "
              f"({100.0 * (syn_cost or 0) / (cost or 1):.0f}% of window spend, excluded from run-rate)")
    print(f"window        ${cost:.4f} spend / {reqs} reqs; last 7 days agent spend ${week_actual:.2f}")
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
