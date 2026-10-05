# Shared helpers for the OpenClaw Router launchers.
$Base    = 'http://127.0.0.1:6060'
$TaskName = 'OpenClaw Router'
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Url($path) {
    try { return (Invoke-WebRequest "$Base$path" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}

function Wait-Router($seconds = 25) {
    for ($i = 0; $i -lt $seconds; $i++) { if (Test-Url '/health') { return $true }; Start-Sleep 1 }
    return $false
}

function Start-Router {
    # Prefer the scheduled task (same way the router normally starts); fall back to the hidden launcher.
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Start-ScheduledTask -TaskName $TaskName }
    else { Start-Process wscript.exe -ArgumentList ('"{0}"' -f (Join-Path $Root 'router-hidden.vbs')) -WindowStyle Hidden }
}

function Stop-Router {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort 6060 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
}

function Log($m) {
    try { Add-Content -Path (Join-Path $Root "launcher.log") -Value ("{0}  {1}" -f (Get-Date -Format s), $m) } catch {}
}

function Show-Msg($m) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($m, "Model Router")
}
