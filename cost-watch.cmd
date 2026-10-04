@echo off
rem Entry point for the "OpenClaw Router Cost Watch" scheduled task.
rem All logic lives in cost-watch-run.ps1 - batch error handling is the wrong tool here.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cost-watch-run.ps1"
