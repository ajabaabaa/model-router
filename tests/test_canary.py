"""Canary quality suite: graders, runner, results store, and the control API around it."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import canary
import config_store
import dashboard
from test_dashboard import use_db

GUARD = {"host": "127.0.0.1:6060", "x-control-action": "1"}
TASK = {t["id"]: t for t in canary.TASKS}


def grade(task_id, text, msg=None):
    return TASK[task_id]["check"](text, msg or {})


# ---------------------------------------------------------------- graders

def test_ids_are_unique_and_prompts_are_synthetic_strings():
    assert len(canary.TASK_IDS) == len(set(canary.TASK_IDS)) == 14
    assert all(isinstance(t["messages"][0]["content"], str) for t in canary.TASKS)


def test_extraction_scores_per_field_and_tolerates_a_code_fence():
    good = '```json\n{"vendor":"Northwind Traders","invoice_number":"INV-20417","total":1284.50,"due_date":"2026-11-15"}\n```'
    assert grade("extract-invoice", good)[0] == 1.0
    half = '{"vendor":"Northwind Traders","invoice_number":"INV-1","total":"$1,284.50","due_date":"15 Nov"}'
    assert grade("extract-invoice", half)[0] == 0.5
    assert grade("extract-invoice", "I could not parse that")[0] == 0.0


def test_reasoning_blocks_are_ignored_by_graders():
    assert grade("math-word", "<think>maybe 99</think> The total is 25.27")[0] == 1.0
    assert grade("math-word", "It is 25.27 or maybe 30")[0] == 0.0


def test_logic_puzzle_has_a_single_answer():
    assert grade("logic-puzzle", "Reasoning...\nCarol")[0] == 1.0
    assert grade("logic-puzzle", "Reasoning...\nAlice")[0] == 0.0


def test_code_tasks_run_tests_and_block_dangerous_code():
    fixed = "```python\ndef moving_average(xs, k):\n    return [sum(xs[i:i+k])/k for i in range(len(xs)-k+1)]\n```"
    assert grade("code-bugfix", fixed)[0] == 1.0
    assert grade("code-bugfix", "```python\ndef moving_average(xs, k):\n    return []\n```")[0] == 0.0
    score, why = grade("code-bugfix", "```python\nimport os\ndef moving_average(xs, k):\n    return []\n```")
    assert score == 0.0 and "blocked" in why
    score, why = grade("code-bugfix", "```python\nwhile True: pass\n```")  # no function, but must never hang the suite
    assert score == 0.0


def test_python_runner_times_out(monkeypatch):
    ok, why = canary.run_python("while True: pass", "", timeout=1)
    assert not ok and why == "timed out"


def test_sql_is_graded_by_result_and_must_be_one_select():
    q = "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status='paid' GROUP BY c.region;"
    assert grade("sql-region", q)[0] == 1.0
    assert grade("sql-region", "SELECT region, SUM(amount) FROM orders GROUP BY region")[0] == 0.0
    assert grade("sql-region", "DROP TABLE orders")[0] == 0.0
    assert grade("sql-region", "SELECT 1; DROP TABLE orders")[0] == 0.0


def test_formatting_and_redaction_checks():
    assert grade("format-bullets", "- Backups protect data\n- They speed recovery\n- Test restores often")[0] == 1.0
    assert grade("format-bullets", "- Backups are very important\n- b\n- c")[0] == 0.0
    assert grade("redact-emails", "Hi team, please send the invoice to [EMAIL] and copy [EMAIL] by Thursday.")[0] == 1.0
    assert grade("redact-emails", "please send the invoice to ana.lopez@example.com by Thursday")[0] < 0.5


def test_json_only_rejects_prose_and_fences():
    ok = '{"city":"Lisbon","country":"Portugal","population_millions":0.55}'
    assert grade("json-only", ok)[0] == 1.0
    assert grade("json-only", "```json\n" + ok + "\n```")[0] == 0.0


def test_tool_call_grading():
    def call(name, args):
        return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(args)}}]}
    assert grade("tool-call", "", call("get_weather", {"city": "Lisbon", "unit": "celsius"}))[0] == 1.0
    assert grade("tool-call", "", call("get_weather", {"city": "Porto", "unit": "celsius"}))[0] == 0.5
    assert grade("tool-call", "", call("other", {}))[0] == 0.25
    assert grade("tool-call", "It is sunny", {})[0] == 0.0


def test_needle_prompts_differ_in_size_and_contain_the_needle_once():
    short, long_ = (TASK[i]["messages"][0]["content"] for i in ("needle-4k", "needle-16k"))
    assert len(long_) > 3 * len(short) and short.count(canary.NEEDLE) == long_.count(canary.NEEDLE) == 1
    assert grade("needle-4k", "4817-QX")[0] == 1.0 and grade("needle-16k", "I don't see one")[0] == 0.0


# ---------------------------------------------------------------- runner

def fake_router(answers, status=200, cost=0.001):
    def handler(request):
        body = json.loads(request.content)
        assert request.headers["x-router-client"] == "benchmark-canary" and body["temperature"] == 0
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        task = next(t for t in canary.TASKS if t["messages"] == body["messages"])
        msg = answers.get(task["id"], {"content": "no idea"})
        return httpx.Response(200, json={"model": "vendor/real-model", "choices": [{"message": msg}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": cost}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_run_task_records_score_latency_cost_and_answering_model():
    row = canary.run_task("fast", TASK["json-only"], fake_router(
        {"json-only": {"content": '{"city":"Lisbon","country":"Portugal","population_millions":0.55}'}}))
    assert row["score"] == 1.0 and row["answered_by"] == "vendor/real-model" and row["cost"] == 0.001
    assert row["input_tokens"] == 10 and row["latency_ms"] is not None and row["error"] is None


def test_run_task_never_raises_on_router_errors_or_network_failure():
    assert canary.run_task("fast", TASK["json-only"], fake_router({}, status=500))["error"] == "HTTP 500"
    def boom(request): raise httpx.ConnectError("down")
    row = canary.run_task("fast", TASK["json-only"], httpx.Client(transport=httpx.MockTransport(boom)))
    assert row["error"] == "ConnectError" and row["score"] == 0.0


def test_prompts_and_answers_are_not_stored(tmp_path):
    row = canary.run_task("fast", TASK["json-only"], fake_router({"json-only": {"content": "SECRET-ANSWER"}}))
    path = tmp_path / "r.jsonl"; canary.append_results([row], path)
    stored = path.read_text()
    assert "SECRET-ANSWER" not in stored and "Lisbon" not in stored


def test_summary_uses_latest_result_per_task_and_ranks_by_score(tmp_path):
    def r(tier, task, score, cost=0.0, ts="2026-10-04T10:00:00+00:00", lat=1000):
        return {"ts": ts, "tier": tier, "task": task, "type": "x", "score": score, "detail": "", "latency_ms": lat,
                "cost": cost, "answered_by": f"{tier}-model", "error": None}
    rows = [r("fast", "json-only", 0.0, ts="2026-10-04T09:00:00+00:00"), r("fast", "json-only", 1.0, 0.01),
            r("fast", "tool-call", 1.0, 0.01), r("local", "json-only", 0.5), {"junk": 1}]
    path = tmp_path / "r.jsonl"; canary.append_results(rows[:-1], path)
    path.write_text(path.read_text() + "not json\n")
    s = canary.summarize(canary.read_results(path))
    fast = next(t for t in s["tiers"] if t["tier"] == "fast")
    assert fast["completed"] == 2 and fast["points"] == 2.0 and fast["cost"] == 0.02
    assert fast["points_per_dollar"] == 100.0
    local = next(t for t in s["tiers"] if t["tier"] == "local")
    assert local["points_per_dollar"] is None
    assert s["tiers"][0]["tier"] == "fast" or s["tiers"][0]["score"] >= s["tiers"][1]["score"]


def test_runner_runs_one_batch_at_a_time(tmp_path, monkeypatch):
    gate = {"release": False}
    def slow(tier, task, client, url=None):
        while not gate["release"]: time.sleep(0.01)
        return {"ts": "t", "tier": tier, "task": task["id"], "type": task["type"], "score": 1.0}
    monkeypatch.setattr(canary, "run_task", slow)
    runner = canary.Runner()
    assert runner.start(["fast"], ["json-only", "tool-call"], path=tmp_path / "r.jsonl")
    assert not runner.start(["fast"], path=tmp_path / "r.jsonl") and runner.state["total"] == 2
    gate["release"] = True
    for _ in range(200):
        if not runner.state["running"]: break
        time.sleep(0.01)
    assert runner.state["done"] == 2 and len((tmp_path / "r.jsonl").read_text().splitlines()) == 2


# ---------------------------------------------------------------- API

@pytest.fixture
def api(monkeypatch, tmp_path):
    cfg = {"tiers": {"fast": {"provider": "openrouter", "primary": "a/cheap", "fallbacks": [], "provider_policy": {"zdr": True}},
                     "local": {"provider": "ollama", "primary": "qwen3.5:9b", "fallbacks": [], "provider_policy": {"local": True}}},
           "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_KEY"}}
    path = tmp_path / "router_config.json"; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config_store, "CONFIG_PATH", path); monkeypatch.setattr(dashboard, "CONFIG_PATH", path)
    monkeypatch.setattr(config_store, "BACKUP_DIR", tmp_path / "b"); monkeypatch.setattr(config_store, "AUDIT_PATH", tmp_path / "a.jsonl")
    monkeypatch.setattr(canary, "RESULTS", tmp_path / "canary.jsonl")
    monkeypatch.setattr(canary, "RUNNER", canary.Runner())
    started = []
    monkeypatch.setattr(canary.RUNNER, "start", lambda tiers, ids=None, url="", path=None: started.append((tiers, ids, url)) or True)
    return TestClient(app.app), started


def test_status_endpoint_lists_tiers_and_providers(api):
    client, _ = api
    j = client.get("/api/control/canary").json()
    assert j["tiers"]["local"]["provider"] == "ollama" and j["task_count"] == 14 and j["status"]["running"] is False


def test_run_is_guarded_like_every_other_write(api):
    client, started = api
    assert client.post("/api/control/canary/run", json={"tiers": ["local"]}, headers={"host": "127.0.0.1:6060"}).status_code == 403
    assert client.post("/api/control/canary/run", json={"tiers": ["local"]}, headers={**GUARD, "origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/control/canary/run", json={"tiers": ["local"]}, headers={**GUARD, "host": "evil.example"}).status_code == 403
    assert not started


def test_run_validates_tiers_and_tasks(api):
    client, started = api
    for body in ({}, {"tiers": []}, {"tiers": ["nope"]}, {"tiers": ["local"], "tasks": ["nope"]}, {"tiers": "local"}, []):
        assert client.post("/api/control/canary/run", json=body, headers=GUARD).status_code == 400
    assert not started


def test_cloud_tiers_need_confirmation_but_local_do_not(api):
    client, started = api
    r = client.post("/api/control/canary/run", json={"tiers": ["fast", "local"]}, headers=GUARD)
    assert r.status_code == 409 and r.json()["error"] == "confirm_required" and not started
    assert client.post("/api/control/canary/run", json={"tiers": ["local"]}, headers=GUARD).status_code == 200
    assert client.post("/api/control/canary/run", json={"tiers": ["fast"], "confirm": True}, headers=GUARD).status_code == 200
    assert started[0][0] == ["local"] and started[1][0] == ["fast"]
    assert started[0][2] == "http://127.0.0.1:6060/v1/chat/completions"


def test_a_second_run_while_one_is_active_is_refused(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(canary.RUNNER, "start", lambda *a, **k: False)
    assert client.post("/api/control/canary/run", json={"tiers": ["local"]}, headers=GUARD).status_code == 409


def test_canary_label_counts_as_synthetic_traffic():
    assert dashboard._agent_kind(canary.CLIENT_LABEL) == "synthetic"
