"""Canary quality suite: a fixed set of small tasks with machine-checkable answers.

Each task is sent through the router like any client would send it (model = tier name), so the
result reflects the whole chain: tier, fallbacks and provider policy. Every prompt is synthetic
(written here, never taken from the operator's traffic) and every answer is graded by code, not
by another model, so a score means "the check passed", not "a judge liked it".

Results are appended to canary_results.jsonl. Requests carry the label "benchmark-canary", which
the dashboard already treats as synthetic traffic, so canary runs stay out of real caller stats.

Safety: two checks execute model output. SQL runs read-only in an in-memory SQLite database. Python
is statically screened (no imports of os/sys/subprocess/etc., no open/eval/exec/__import__) and then
run with `python -I` in a throwaway directory with a short timeout. That screen is a tripwire for
honest mistakes, not a security boundary; do not point the suite at models you do not trust to
write harmless code.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).parent
RESULTS = ROOT / "canary_results.jsonl"
ROUTER_URL = "http://127.0.0.1:6060/v1/chat/completions"
CLIENT_LABEL = "benchmark-canary"
MAX_TOKENS = 1500
REQUEST_TIMEOUT = 300.0

Check = Callable[[str, dict], "tuple[float, str]"]


# ---------------------------------------------------------------- helpers

def strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def find_json(text: str) -> Any:
    """First JSON object or array in the text; models often wrap it in prose or a code fence."""
    text = strip_reasoning(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for chunk in candidates:
        for i, ch in enumerate(chunk):
            if ch in "[{":
                try:
                    return decoder.raw_decode(chunk[i:])[0]
                except ValueError:
                    continue
    raise ValueError("no JSON found")


def code_block(text: str) -> str:
    text = strip_reasoning(text)
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S)
    return (m.group(1) if m else text).strip()


BANNED = re.compile(r"\b(import\s+(os|sys|subprocess|shutil|socket|pathlib|ctypes|requests|urllib|http)\b|"
                    r"from\s+(os|sys|subprocess|shutil|socket|pathlib|ctypes)\b|open\s*\(|eval\s*\(|exec\s*\(|"
                    r"__import__|compile\s*\()")


def run_python(code: str, tests: str, timeout: float = 10.0) -> tuple[bool, str]:
    if BANNED.search(code):
        return False, "code uses a blocked construct (import/open/eval); not executed"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "t.py"
        script.write_text(code + "\n\n" + tests, encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, "-I", str(script)], cwd=tmp, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timed out"
    if done.returncode == 0:
        return True, "tests passed"
    last = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["failed"]
    return False, last[0][:160]


def words(text: str) -> list[str]:
    return re.findall(r"\b[\w'-]+\b", text)


# ---------------------------------------------------------------- task checks

def check_extract(text: str, msg: dict) -> tuple[float, str]:
    try:
        data = find_json(text)
    except ValueError:
        return 0.0, "no valid JSON"
    want = {"vendor": "Northwind Traders", "invoice_number": "INV-20417", "total": 1284.5, "due_date": "2026-11-15"}
    got = 0
    for key, expected in want.items():
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(expected, float):
            try:
                got += abs(float(str(value).replace(",", "").replace("$", "")) - expected) < 0.005
            except ValueError:
                pass
        else:
            got += str(value).strip() == expected
    return got / len(want), f"{got}/{len(want)} fields correct"


CLASSIFY_ITEMS = [("The checkout page crashed and I lost my cart.", "bug"),
                  ("Could you add dark mode to the dashboard?", "feature"),
                  ("How do I export my data to CSV?", "question"),
                  ("Charged twice for the same order, please refund one.", "billing"),
                  ("The export button does nothing when I click it.", "bug"),
                  ("Do you support single sign-on with Okta?", "question")]


def check_classify(text: str, msg: dict) -> tuple[float, str]:
    try:
        data = find_json(text)
    except ValueError:
        return 0.0, "no valid JSON"
    if not isinstance(data, list):
        return 0.0, "expected a JSON array"
    got = sum(1 for i, (_, label) in enumerate(CLASSIFY_ITEMS)
              if i < len(data) and str(data[i]).strip().lower() == label)
    return got / len(CLASSIFY_ITEMS), f"{got}/{len(CLASSIFY_ITEMS)} labels correct"


def check_summary(text: str, msg: dict) -> tuple[float, str]:
    body = strip_reasoning(text).lower()
    facts = ["14 march", "3.2 million", "two weeks"]
    hit = sum(f in body or f.replace("14 march", "march 14") in body for f in facts)
    short = len(words(body)) <= 60
    return (hit / len(facts)) * (1.0 if short else 0.5), f"{hit}/3 facts, {len(words(body))} words"


def number_check(expected: float, tol: float = 0.01) -> Check:
    def check(text: str, msg: dict) -> tuple[float, str]:
        body = strip_reasoning(text)
        nums = [float(n.replace(",", "")) for n in re.findall(r"-?\d[\d,]*\.?\d*", body)]
        if nums and abs(nums[-1] - expected) <= tol:
            return 1.0, f"answered {nums[-1]:g}"
        return 0.0, f"expected {expected:g}, got {nums[-1]:g}" if nums else "no number"
    return check


def check_logic(text: str, msg: dict) -> tuple[float, str]:
    body = strip_reasoning(text).lower()
    last = body.splitlines()[-1] if body else ""
    return (1.0, "correct") if "carol" in last or ("carol" in body and body.count("carol") > body.count("alice") + body.count("bob")) else (0.0, "wrong person")


def check_phone(text: str, msg: dict) -> tuple[float, str]:
    tests = """
assert normalize_phone('(704) 555-0199') == '+17045550199'
assert normalize_phone('704.555.0199') == '+17045550199'
assert normalize_phone('1-704-555-0199') == '+17045550199'
assert normalize_phone('+1 704 555 0199') == '+17045550199'
assert normalize_phone('555-0199') is None
assert normalize_phone('') is None
"""
    ok, why = run_python(code_block(text), tests)
    return (1.0 if ok else 0.0), why


def check_bugfix(text: str, msg: dict) -> tuple[float, str]:
    tests = """
assert moving_average([1, 2, 3, 4, 5], 3) == [2.0, 3.0, 4.0]
assert moving_average([10, 20], 2) == [15.0]
assert moving_average([1, 2], 3) == []
assert moving_average([4], 1) == [4.0]
"""
    ok, why = run_python(code_block(text), tests)
    return (1.0 if ok else 0.0), why


def check_sql(text: str, msg: dict) -> tuple[float, str]:
    query = code_block(text).strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", query) or ";" in query:
        return 0.0, "not a single SELECT"
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT);
            INSERT INTO customers VALUES (1,'Ada','east'),(2,'Ben','west'),(3,'Cy','east'),(4,'Di','west');
            INSERT INTO orders VALUES (1,1,100,'paid'),(2,1,50,'paid'),(3,2,70,'paid'),(4,3,300,'refunded'),
                                      (5,3,20,'paid'),(6,2,10,'paid'),(7,4,500,'paid'),(8,4,5,'cancelled');
        """)
        rows = db.execute(query).fetchall()
    except sqlite3.Error as exc:
        return 0.0, f"sql error: {str(exc)[:80]}"
    finally:
        db.close()
    flat = {tuple(str(v).lower() if isinstance(v, str) else round(float(v), 2) for v in row) for row in rows}
    want_a = {("west", 580.0), ("east", 170.0)}
    return (1.0, "correct") if flat == want_a else (0.0, f"wrong result ({len(rows)} rows)")


def check_format(text: str, msg: dict) -> tuple[float, str]:
    body = strip_reasoning(text)
    lines = [l for l in body.splitlines() if l.strip()]
    bullets = [l for l in lines if re.match(r"^\s*[-*•]\s+\S", l)]
    problems = []
    if len(lines) != 3 or len(bullets) != 3:
        problems.append("need exactly 3 bullet lines")
    if any(len(words(re.sub(r"^\s*[-*•]\s+", "", l))) > 12 for l in bullets):
        problems.append("a bullet exceeds 12 words")
    if re.search(r"\bvery\b", body, flags=re.I):
        problems.append("used the banned word")
    return (0.0 if problems else 1.0), "; ".join(problems) or "format respected"


def check_redact(text: str, msg: dict) -> tuple[float, str]:
    body = strip_reasoning(text)
    leaked = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body)
    placeholders = body.count("[EMAIL]")
    kept = "invoice" in body.lower() and "Thursday" in body
    score = (0.0 if leaked else 0.5) + (0.25 if placeholders == 2 else 0.0) + (0.25 if kept else 0.0)
    return score, f"{len(leaked)} emails left, {placeholders} placeholders"


def check_json_only(text: str, msg: dict) -> tuple[float, str]:
    body = strip_reasoning(text).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return 0.0, "reply is not bare JSON"
    ok = isinstance(data, dict) and sorted(data) == ["city", "country", "population_millions"] \
        and isinstance(data["population_millions"], (int, float))
    return (1.0, "bare JSON, right keys") if ok else (0.5, "bare JSON, wrong shape")


def check_tool_call(text: str, msg: dict) -> tuple[float, str]:
    calls = (msg or {}).get("tool_calls") or []
    if not calls:
        return 0.0, "no tool call made"
    fn = (calls[0].get("function") or {})
    if fn.get("name") != "get_weather":
        return 0.25, f"called {fn.get('name')}"
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return 0.5, "arguments are not valid JSON"
    city = str(args.get("city", "")).lower()
    return (1.0, "correct call") if "lisbon" in city and args.get("unit") in ("celsius", "c") else (0.5, f"args {args}")


NEEDLE = "The vault code for project Heron is 4817-QX."


def filler(word_count: int, seed: int) -> tuple[str, int]:
    base = ["The committee reviewed the quarterly logistics schedule and noted routine adjustments.",
            "Maintenance windows were moved to accommodate regional holidays and staffing.",
            "Supplier lead times remained stable across most categories during the period.",
            "Warehouse throughput improved slightly after the layout change on the second floor.",
            "Training sessions for the new scanning workflow will continue through the month.",
            "Budget variance stayed within the expected range for travel and equipment.",
            "The audit team requested copies of the revised retention procedures.",
            "No safety incidents were reported at any of the three distribution sites."]
    out, count, i = [], 0, seed
    while count < word_count:
        s = base[i % len(base)] + f" (ref {i})"
        out.append(s)
        count += len(s.split())
        i += 3
    return " ".join(out), count


def needle_prompt(word_count: int) -> str:
    half = word_count // 2
    a, _ = filler(half, 1)
    b, _ = filler(word_count - half, 2)
    return (f"Read the following notes, then answer the question at the end.\n\n{a}\n\n{NEEDLE}\n\n{b}\n\n"
            "Question: What is the vault code for project Heron? Reply with the code only.")


def check_needle(text: str, msg: dict) -> tuple[float, str]:
    return (1.0, "found the code") if "4817-QX" in strip_reasoning(text) else (0.0, "code not found (truncated or missed)")


# ---------------------------------------------------------------- the suite

def _user(content: str) -> list[dict]:
    return [{"role": "user", "content": content}]


TASKS: list[dict[str, Any]] = [
    {"id": "extract-invoice", "type": "extraction", "check": check_extract,
     "messages": _user("Extract fields from this text and reply with JSON only, using exactly the keys "
                       "vendor, invoice_number, total (number), due_date (YYYY-MM-DD).\n\n"
                       "Invoice INV-20417 from Northwind Traders. Amount due: $1,284.50. Payment is due by 15 November 2026.")},
    {"id": "classify-tickets", "type": "classification", "check": check_classify,
     "messages": _user("Classify each support message as one of: bug, feature, question, billing. Reply with a JSON array "
                       "of labels in order, nothing else.\n\n" + "\n".join(f"{i + 1}. {t}" for i, (t, _) in enumerate(CLASSIFY_ITEMS)))},
    {"id": "summarize-facts", "type": "summarization", "check": check_summary,
     "messages": _user("Summarize in at most 50 words, keeping the date, the number and the timeframe:\n\n"
                       "On 14 March the city council approved a 3.2 million dollar plan to resurface the riverside cycle path. "
                       "Work is expected to take two weeks, after which the path will reopen with new lighting. Council members "
                       "debated the cost for several hours, and a neighbourhood group asked for better signage during the closure.")},
    {"id": "math-word", "type": "reasoning", "check": number_check(25.27, 0.011),
     "messages": _user("A shop sells pens at 3 for $4.50. Maya buys 24 pens. Tax of 8% is added to the full price, and "
                       "then a coupon takes 35% off the taxed total. How much does she pay, in dollars to two decimals? "
                       "End your reply with just the number.")},
    {"id": "logic-puzzle", "type": "reasoning", "check": check_logic,
     "messages": _user("Alice, Bob and Carol each own exactly one pet: a cat, a dog or a fish. Alice does not own the dog. "
                       "The person with the fish is not Bob. Bob does not own the cat. Carol does not own the cat. Who owns the fish? "
                       "End your reply with just the name.")},
    {"id": "code-phone", "type": "coding", "check": check_phone,
     "messages": _user("Write a Python function normalize_phone(s) that converts a US phone number string to E.164 "
                       "(+1XXXXXXXXXX). Accept 10 digits or 11 digits starting with 1, ignoring spaces, dots, dashes, "
                       "parentheses and a leading +. Return None for anything else. Reply with one python code block, no imports.")},
    {"id": "code-bugfix", "type": "coding", "check": check_bugfix,
     "messages": _user("This function should return the moving average of every window of size k, as floats, and [] if the "
                       "list is shorter than k. Fix it and reply with one python code block, no imports.\n\n"
                       "```python\ndef moving_average(xs, k):\n    out = []\n    for i in range(len(xs) - k):\n"
                       "        out.append(sum(xs[i:i + k]) / k)\n    return out\n```")},
    {"id": "sql-region", "type": "coding", "check": check_sql,
     "messages": _user("SQLite tables: customers(id, name, region), orders(id, customer_id, amount, status). "
                       "Write one SELECT that returns each region and the total amount of orders with status 'paid' for "
                       "that region, as two columns (region, total). Reply with the query only.")},
    {"id": "format-bullets", "type": "instruction-following", "check": check_format,
     "messages": _user("Give exactly 3 bullet points (lines starting with '- ') about why backups matter. Each bullet must "
                       "be 12 words or fewer. Do not use the word 'very'. No other text.")},
    {"id": "redact-emails", "type": "instruction-following", "check": check_redact,
     "messages": _user("Rewrite this message replacing every email address with [EMAIL]. Keep everything else unchanged and "
                       "reply with the message only.\n\nHi team, please send the invoice to ana.lopez@example.com and copy "
                       "billing-ops@example.org by Thursday.")},
    {"id": "json-only", "type": "instruction-following", "check": check_json_only,
     "messages": _user("Reply with a bare JSON object (no code fence, no prose) with keys city, country and "
                       "population_millions (a number) describing Lisbon.")},
    {"id": "tool-call", "type": "tool-use", "check": check_tool_call,
     "messages": _user("What is the weather in Lisbon right now? Use celsius."),
     "extra": {"tools": [{"type": "function", "function": {
         "name": "get_weather", "description": "Get the current weather for a city.",
         "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
                        "required": ["city", "unit"]}}}], "tool_choice": "auto"}},
    {"id": "needle-4k", "type": "long-context", "check": check_needle, "messages": _user(needle_prompt(2600))},
    {"id": "needle-16k", "type": "long-context", "check": check_needle, "messages": _user(needle_prompt(11000))},
]
TASK_IDS = [t["id"] for t in TASKS]
# math-word: 24 pens at $4.50 per 3 = $36.00; +8% tax = 38.88; -35% = 25.272


# ---------------------------------------------------------------- running

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_task(tier: str, task: dict[str, Any], client: httpx.Client, url: str = ROUTER_URL) -> dict[str, Any]:
    body = {"model": tier, "messages": task["messages"], "temperature": 0, "max_tokens": MAX_TOKENS, **task.get("extra", {})}
    row: dict[str, Any] = {"ts": _now(), "tier": tier, "task": task["id"], "type": task["type"], "score": 0.0,
                           "detail": "", "latency_ms": None, "input_tokens": None, "output_tokens": None,
                           "cost": None, "answered_by": None, "error": None}
    started = time.perf_counter()
    try:
        resp = client.post(url, json=body, headers={"X-Router-Client": CLIENT_LABEL}, timeout=REQUEST_TIMEOUT)
        row["latency_ms"] = round((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            row["error"] = f"HTTP {resp.status_code}"
            row["detail"] = "router returned an error"
            return row
        data = resp.json()
        msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        usage = data.get("usage") or {}
        row.update(answered_by=data.get("model"), input_tokens=usage.get("prompt_tokens"),
                   output_tokens=usage.get("completion_tokens"), cost=usage.get("cost"))
        score, detail = task["check"](msg.get("content") or "", msg)
        row["score"], row["detail"] = round(float(score), 3), detail
    except Exception as exc:  # network error, timeout, malformed body: record, never raise into the batch
        row["latency_ms"] = row["latency_ms"] or round((time.perf_counter() - started) * 1000)
        row["error"] = type(exc).__name__
        row["detail"] = str(exc)[:120]
    return row


def append_results(rows: list[dict[str, Any]], path: Path | None = None) -> None:
    path = path or RESULTS
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_results(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or RESULTS
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("tier") and row.get("task"):
            rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]], task_ids: list[str] | None = None) -> dict[str, Any]:
    """Latest result per (tier, task), then per-tier totals. Old runs are history, not averaged in."""
    wanted = set(task_ids or TASK_IDS)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row["task"] in wanted:
            latest[(row["tier"], row["task"])] = row
    tiers: dict[str, dict[str, Any]] = {}
    for (tier, task), row in latest.items():
        t = tiers.setdefault(tier, {"tier": tier, "tasks": {}, "scores": [], "latency": [], "cost": 0.0,
                                    "answered_by": {}, "errors": 0, "last_run": ""})
        t["tasks"][task] = {"score": row["score"], "detail": row.get("detail"), "error": row.get("error"),
                            "latency_ms": row.get("latency_ms"), "answered_by": row.get("answered_by")}
        t["scores"].append(row["score"])
        if row.get("latency_ms") is not None:
            t["latency"].append(row["latency_ms"])
        t["cost"] += float(row.get("cost") or 0)
        if row.get("answered_by"):
            t["answered_by"][row["answered_by"]] = t["answered_by"].get(row["answered_by"], 0) + 1
        t["errors"] += 1 if row.get("error") else 0
        t["last_run"] = max(t["last_run"], row.get("ts") or "")
    out = []
    for t in tiers.values():
        n = len(t["scores"])
        lat = sorted(t["latency"])
        mean = sum(t["scores"]) / n if n else 0.0
        out.append({"tier": t["tier"], "tasks": t["tasks"], "completed": n, "of": len(wanted),
                    "score": round(mean, 3), "points": round(sum(t["scores"]), 2),
                    "median_latency_ms": lat[len(lat) // 2] if lat else None,
                    "cost": round(t["cost"], 5), "answered_by": t["answered_by"], "errors": t["errors"],
                    "last_run": t["last_run"],
                    "points_per_dollar": round(sum(t["scores"]) / t["cost"], 1) if t["cost"] > 0 else None})
    out.sort(key=lambda r: (-r["score"], r["cost"]))
    return {"tiers": out, "tasks": [{"id": t["id"], "type": t["type"]} for t in TASKS if t["id"] in wanted]}


class Runner:
    """One background run at a time; progress is readable while it works."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False}

    def start(self, tiers: list[str], task_ids: list[str] | None = None, url: str = ROUTER_URL,
              path: Path | None = None) -> bool:
        with self._lock:
            if self.state.get("running"):
                return False
            tasks = [t for t in TASKS if not task_ids or t["id"] in task_ids]
            self.state = {"running": True, "run_id": uuid.uuid4().hex[:8], "tiers": tiers, "total": len(tiers) * len(tasks),
                          "done": 0, "current": None, "started": _now(), "finished": None, "error": None}
        threading.Thread(target=self._work, args=(tiers, tasks, url, path), daemon=True).start()
        return True

    def _work(self, tiers: list[str], tasks: list[dict[str, Any]], url: str, path: Path) -> None:
        try:
            with httpx.Client() as client:
                for tier in tiers:
                    for task in tasks:
                        self.state["current"] = f"{tier} · {task['id']}"
                        append_results([run_task(tier, task, client, url)], path)
                        self.state["done"] += 1
        except Exception as exc:
            self.state["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            self.state.update(running=False, current=None, finished=_now())


RUNNER = Runner()


if __name__ == "__main__":  # python canary.py fast balanced
    chosen = sys.argv[1:] or ["fast"]
    with httpx.Client() as http:
        collected = []
        for tier_name in chosen:
            for spec in TASKS:
                result = run_task(tier_name, spec, http)
                collected.append(result)
                print(f"{tier_name:12} {spec['id']:16} {result['score']:.2f}  {result['detail']}")
        append_results(collected)
