# One-time setup: runs the egress monitor at every logon (hidden) and starts it now.
$Root = 'C:\dev\openclaw-router'
$name = 'Model Router Egress Monitor'
$ps   = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
# Stop any copy that is already running so the new script version takes over.
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'egress-collector\.ps1' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
$action   = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f (Join-Path $Root 'egress-collector.ps1')) -WorkingDirectory $Root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Description 'Records which programs connect to AI providers (metadata only).' -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Registered and started '$name'. Open the Egress tab in the Control Center in a minute."
