# Restarts the router (stops the scheduled task and anything on port 6060, then starts it again).
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'router-common.ps1')
Stop-Router
Start-Router
$ok = Wait-Router 30
Add-Type -AssemblyName System.Windows.Forms
$msg = if ($ok) { 'Router restarted and healthy.' } else { 'Router did not come back. Check C:\dev\openclaw-router\router.log' }
[void][System.Windows.Forms.MessageBox]::Show($msg, 'OpenClaw Router')
