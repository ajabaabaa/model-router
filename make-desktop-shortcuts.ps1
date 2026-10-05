# Run once. Creates two Desktop shortcuts: "Router Control Center" and "Restart Router".
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$icon    = Join-Path $root 'control-center.ico'
$shell   = New-Object -ComObject WScript.Shell

function New-Shortcut($name, $script, $description) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$name.lnk"))
    $lnk.TargetPath       = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $root $script)`""
    $lnk.WorkingDirectory = $root
    $lnk.Description      = $description
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    "created: $($lnk.FullName)"
}

New-Shortcut 'Router Control Center' 'launch-control-center.ps1' 'Open the OpenClaw Router Control Center (starts the router if needed)'
New-Shortcut 'Restart Router'        'restart-router.ps1'        'Restart the OpenClaw router'
