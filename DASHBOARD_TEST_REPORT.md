# Router Dashboard Validation Report

Date: 2026-09-29

## Summary

Dashboard APIs, data calculations, privacy boundaries, and read-only behavior passed validation. The one authorized live FAST request succeeded and appeared in telemetry and refreshed dashboard data. Full test suite: **56 passed**, with one dependency deprecation warning. Browser automation could not initialize, so visual rendering, browser-console inspection, and actual interactive control operation remain **unverified**; no visual defect was established from static inspection.

No files were changed in the router implementation. This report is the only file created. The router was left running.

## 1. Application startup — PASS

Exact command used:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 6060
```

The router reported healthy at `http://127.0.0.1:6060/health`:

```json
{"router_running":true,"openrouter_configured":true,"database_available":true}
```

Process inspection found one intended Python/Uvicorn process listening on `127.0.0.1:6060` (PID 34992). `/dashboard` returned HTTP 200.

## 2. Dashboard page — PARTIAL (browser visual validation unavailable)

URL tested: `http://127.0.0.1:6060/dashboard` — HTTP 200. The page and its inline JS/CSS are served. The local Chart.js asset at `http://127.0.0.1:6060/dashboard/vendor/chart.umd.min.js` returned HTTP 200 (205,399 bytes), so the dashboard does not depend on that chart library loading from a remote host.

Static source inspection found chart canvases, populated summary/table rendering paths, a 25/50/100/500 selector, a manual Refresh control, checked 15-second auto-refresh, a health indicator, empty-state handling, responsive grid styling, and a horizontally scrollable recent table. API requests for both 25 and 100 rows succeeded.

The computer-use/browser automation runtime failed to initialize. Therefore chart rendering, actual clicks/auto-refresh timing, browser-console errors, and 1920×1080 overlap/readability were not directly observed. No static evidence of broken JS/CSS or layout overlap was found; those points remain unverified rather than passed.

## 3. API endpoints — PASS

URLs tested:

- `GET /api/dashboard/summary?recent=25` — HTTP 200, valid JSON, returned window 25.
- `GET /api/dashboard/summary?recent=100` — HTTP 200, valid JSON, returned window 100.
- `GET /api/dashboard/recent?limit=25` — HTTP 200, valid JSON, 25 rows.
- `GET /api/dashboard/recent?limit=100` — HTTP 200, valid JSON, 100 rows.
- Invalid `recent`/`limit` values (`0`, `1001`, and non-numeric text) returned HTTP 422 predictably.

Summary JSON has top-level `window`, `cards`, `health`, `routing`, and `models`. Card fields include request totals, successful/failed counts and rate, latency sample count/mean/median/p95, total/average estimated cost, fallback count/rate, and budget-exhaustion count/rate. Recent rows contain only timestamp, requested/selected tier, automatic-selection flag, routing reason, actual model, latency, estimated cost, HTTP status, success, fallback count, and budget-exhausted status.

The response reports available windows `[25, 50, 100, 500]`; low-volume/empty query paths are guarded by the implementation and have test coverage. A separate visual empty-state review was not possible.

## 4. Data correctness — PASS

Dashboard results were compared with independent read-only SQL over the same latest-N telemetry rows for N=25 and N=100. All compared values matched: total and successful requests/rate, FAST/BALANCED/DEEP counts, routing-reason counts, automatic/explicit counts, average/median/p95 latency, total and average cost, fallback rate, budget-exhaustion rate, and actual-model distribution.

Before the controlled request, the database contained 413 telemetry rows. The baseline latest-window summary values were:

| Window | Requests in window | Success rate | Avg latency | Median latency | p95 latency | Total cost |
|---:|---:|---:|---:|---:|---:|---:|
| 25 | 25 | 100% | 37,601.15 ms | 40,280.14 ms | 64,053.21 ms | 0.30161386 |
| 100 | 100 | 98% | 30,181.69 ms | 26,726.39 ms | 62,216.27 ms | 0.47700194 |

These are windowed figures, not lifetime totals. The live request later became the newest row and the APIs reflected the updated window.

## 5. Privacy — PASS

The API response allowlists and dashboard UI source were inspected. Dashboard payloads do not include prompt text, response text, reasoning, message content, authorization headers, API keys, secrets, or provider credentials. Recent-row keys are restricted to operational metadata. The UI writes returned values as text (not HTML markup). The controlled request's prompt and answer sentinel were absent from all inspected dashboard API payloads.

Source searches found no content-bearing database fields being selected or rendered by dashboard code. The dashboard does expose operational model/tier/routing metadata as intended; it does not expose credentials.

## 6. Read-only behavior — PASS

The dashboard router exposes only GET routes:

- `GET /dashboard`
- `GET /dashboard/vendor/chart.umd.min.js`
- `GET /api/dashboard/summary`
- `GET /api/dashboard/recent`

Queries use read-only database access and SELECT-only reporting paths; no dashboard INSERT/UPDATE/DELETE behavior was found. Telemetry count remained 414 across subsequent dashboard API/page/asset reads. The only count increase during this validation was the one authorized model request (413→414).

## 7. Routing isolation — PASS (with automated failure-isolation coverage)

Dashboard GETs did not add telemetry rows or trigger model requests. `/health` continued to return healthy after the live request and dashboard reads. Existing automated test `test_dashboard_fault_does_not_affect_model_routing` covers dashboard failure isolation using the test harness; no additional completion request was sent for this check.

No benchmark command was run. Exactly one live chat-completion request was sent, as authorized below.

## 8. UI/UX review — PARTIAL

Static inspection found responsive card/chart grids, readable labels and formatted operational metrics, a visible health state, empty-state handling, and a contained/scrollable table. Chart.js is locally served. No definite UI defect was identified.

Not verified in a rendered browser: actual chart appearance, 1920×1080 visual spacing/overlap, browser-console output, broken-resource detection by the browser, control clicks, or auto-refresh behavior. The UI automation runtime failed during initialization. These are validation gaps, not confirmed defects.

## 9. Automated tests — PASS

Command: ` .\.venv\Scripts\python.exe -m pytest -q `

Result: **56 passed in 4.38s**. Dashboard-specific coverage includes summary/recent data, parameter validation, privacy/content exclusion, read-only row-count behavior, asset serving, window selection data, and failure isolation.

Warning: `StarletteDeprecationWarning` from `fastapi.testclient` says using `httpx` with `starlette.testclient` is deprecated and recommends `httpx2`. This is dependency/test-client maintenance advice; tests passed.

## 10. Controlled live-data test — PASS

Exactly one request was sent to `POST http://127.0.0.1:6060/v1/chat/completions` with:

```json
{"model":"fast","messages":[{"role":"user","content":"Reply with exactly: DASHBOARD_TEST_OK"}],"stream":false}
```

It returned HTTP 200; the response matched the requested exact output. The actual response model was `z-ai/glm-5.3-flash`. Exactly one new telemetry row was recorded (413→414), with selected tier `fast` and success true. The refreshed recent API showed the new FAST row first; refreshed summaries returned 25/25 and 100/100 rows and included the new telemetry state. The test prompt/answer were not present in dashboard API responses. Dashboard reads after the request left the count at 414.

No further model requests were made.

## Defects, fixes, and recommendations

- Confirmed dashboard-specific defects: **none**.
- Fixes made: **none**.
- Recommendation: complete a browser-based visual pass when the UI automation runtime is available, especially chart rendering, the four selector choices, manual refresh, the 15-second refresh, and the 1920×1080 layout. Consider updating the test-client dependency stack to address the Starlette deprecation warning separately.

## Overall

Functional API/data/privacy/read-only checks and automated tests **PASS**. Overall end-to-end validation is **PARTIAL** because direct browser rendering and interaction checks were unavailable. The single controlled live-data test passed. No router behavior or dashboard code was changed.
