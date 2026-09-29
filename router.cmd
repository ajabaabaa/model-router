@echo off
rem OpenClaw Router - local model-tier gateway on 127.0.0.1:6060
rem Invoked by scheduled task "OpenClaw Router" via router-hidden.vbs
cd /d "C:\dev\openclaw-router"
"C:\dev\openclaw-router\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 6060 < NUL >> "C:\dev\openclaw-router\router.log" 2>&1
