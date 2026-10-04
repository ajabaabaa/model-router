# Runs the hourly cost watch, appends the report to cost_watch.log, and raises a desktop toast
# only when cost_watch.py signals an alert (exit code 1). A healthy hour stays silent.
# Invoked by cost-watch.cmd, which is what the scheduled task points at.
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Add-Content -Path 'cost_watch.log' -Value "==================== $(Get-Date) ===================="

$report = & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') cost_watch.py 1 2>&1
$code = $LASTEXITCODE
$report | Add-Content -Path 'cost_watch.log'

if ($code -ne 0) {
    $alert = $report | Where-Object { "$_" -match '^ALERT:' } | Select-Object -First 1
    if (-not $alert) { $alert = "cost_watch.py exited $code with no ALERT line - check cost_watch.log" }
    & (Join-Path $PSScriptRoot 'cost-watch-alert.ps1') -Text "$alert"
}

exit $code
