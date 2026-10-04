@echo off
rem Hourly router cost watch, run by the "OpenClaw Router Cost Watch" scheduled task.
rem Appends one timestamped report per run to cost_watch.log (exit 1 means it alerted).
cd /d C:\dev\openclaw-router
echo ==================== %date% %time% ==================== >> cost_watch.log
.venv\Scripts\python.exe cost_watch.py 1 >> cost_watch.log 2>&1
