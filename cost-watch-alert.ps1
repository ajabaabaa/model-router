# Shows a Windows balloon toast for cost_watch alerts. Called by cost-watch.cmd only when
# cost_watch.py exits non-zero, so a healthy hour stays silent.
# Usage: cost-watch-alert.ps1 -Text "first alert line"
param(
    [Parameter(Mandatory = $true)][string]$Text
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Warning
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$icon.BalloonTipTitle = 'openclaw-router cost watch'
# Truncate: the balloon is small and the raw alert text can be long.
if ($Text.Length -gt 180) { $Text = $Text.Substring(0, 177) + '...' }
$icon.BalloonTipText = $Text
$icon.Visible = $true
$icon.ShowBalloonTip(15000)
Start-Sleep -Seconds 3   # the icon must outlive the call for the toast to render
$icon.Dispose()
