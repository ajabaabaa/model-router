. 'C:\dev\openclaw-router\router-common.ps1'
try {
    Log 'launch-control-center clicked'
    try { $eg = Get-ScheduledTask -TaskName 'Model Router Egress Monitor' -ErrorAction Stop; if ($eg.State -ne 'Running') { Start-ScheduledTask -InputObject $eg; Log 'started egress monitor' } } catch {}
    if (-not (Test-Url '/health')) {
        Start-Router; [void](Wait-Router 30)
    }
    elseif (-not (Test-Url '/control')) {
        Log '/control missing: router is on old code, restarting'
        Stop-Router; Start-Router; [void](Wait-Router 30)
    }
    if (Test-Url '/control') { Log 'opening browser'; Start-Process "$Base/control" }
    else { Show-Msg "The Control Center did not come up. Details: C:\dev\openclaw-router\launcher.log and router.log" }
}
catch { Log "ERROR: $($_.Exception.Message)"; Show-Msg "Launcher error: $($_.Exception.Message)" }
