# openclaw-router

Standalone local OpenAI-compatible gateway for testing logical model tiers. It does not connect to OpenClaw, Ollama, or modify existing OpenRouter configuration.

## Install
```powershell
cd C:\dev\openclaw-router
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
The router reads `OPENROUTER_KEY` (or `OPENROUTER_API_KEY`) at runtime and never stores the key.

## Start
```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 6060
```

## Test
```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe smoke_test.py
```

## Example curl
```powershell
curl http://127.0.0.1:6060/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"fast","messages":[{"role":"user","content":"Say hello."}]}'
```

Change model mappings in `router_config.json`. SQLite telemetry is stored at `router_telemetry.db` in this project directory. Prompts and responses are not stored. Optional metadata headers are `X-OpenClaw-Agent`, `X-OpenClaw-Session`, `X-OpenClaw-Project`, and `X-Task-Type`. Aggregate telemetry is available at `GET /telemetry/summary`.
