$ErrorActionPreference = 'Stop'
$name = 'OpenClaw Router'

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument '"C:\dev\openclaw-router\router-hidden.vbs"' `
    -WorkingDirectory 'C:\dev\openclaw-router'

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited `
    -Description 'Local OpenAI-compatible model-tier router for OpenClaw (127.0.0.1:6060).' | Out-Null

$t = Get-ScheduledTask -TaskName $name
"scheduled task registered: $name  state=$($t.State)"
"trigger: $($t.Triggers.CimClass.CimClassName)  user=$($t.Principal.UserId)  runLevel=$($t.Principal.RunLevel)"
"execTimeLimit: $($t.Settings.ExecutionTimeLimit)  restartCount: $($t.Settings.RestartCount)  restartInterval: $($t.Settings.RestartInterval)  multiInst: $($t.Settings.MultipleInstances)"
"action: $($t.Actions.Execute) $($t.Actions.Arguments)"
