. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'router-common.ps1')
$status = Join-Path $Root 'startup-status.js'
function Set-Phase($p, $err = '') {
    $e = $err -replace '[\\"]', '/'
    Set-Content -Path $status -Value ('window.__launcher={phase:"' + $p + '",error:"' + $e + '"};') -Encoding ASCII
}
try {
    Log 'launch-control-center clicked'
    Set-Phase 'launching'
    # Show progress immediately: this page polls the router and forwards to /control when it is ready.
    Start-Process ('file:///' + ((Join-Path $Root 'startup.html') -replace '\\', '/'))
    try { $eg = Get-ScheduledTask -TaskName 'Model Router Egress Monitor' -ErrorAction Stop; if ($eg.State -ne 'Running') { Start-ScheduledTask -InputObject $eg; Log 'started egress monitor' } } catch {}
    if (-not (Test-Url '/health')) {
        Set-Phase 'starting'; Start-Router; [void](Wait-Router 40)
    }
    elseif (-not (Test-Url '/control')) {
        Log '/control missing: router is on old code, restarting'
        Set-Phase 'restarting'; Stop-Router; Start-Router; [void](Wait-Router 40)
    }
    if (Test-Url '/control') { Set-Phase 'ready'; Log 'router ready' }
    else { Set-Phase 'failed' 'The router did not come up. See router.log'; Show-Msg "The Control Center did not come up. Details: C:\dev\openclaw-router\launcher.log and router.log" }
}
catch { Log "ERROR: $($_.Exception.Message)"; Set-Phase 'failed' $_.Exception.Message; Show-Msg "Launcher error: $($_.Exception.Message)" }
