<#
  Direct JSON-RPC client for the Google Stitch MCP endpoint.

  Exists because this Qwen process started before STITCH_API_KEY was set, so the
  mcpServers placeholder cannot resolve here and the native mcp__stitch__* tools fail.
  Reads the key from HKCU\Environment at call time and never prints it; response bodies
  are scrubbed for the key value before display.

  Usage:
    stitch_mcp.ps1 -Method tools/list
    stitch_mcp.ps1 -Method tools/call -ArgsFile body.json
    stitch_mcp.ps1 -Method initialize
#>
param(
    [Parameter(Mandatory = $true)][string]$Method,
    [string]$ArgsFile,
    [string]$ToolName,
    [string]$Url = 'https://stitch.googleapis.com/mcp'
)

$ErrorActionPreference = 'Stop'
$key = [Environment]::GetEnvironmentVariable('STITCH_API_KEY', 'User')
if (-not $key) { Write-Output 'ERROR: STITCH_API_KEY not found in HKCU\Environment'; exit 2 }

$params = @{}
if ($Method -eq 'initialize') {
    $params = @{ protocolVersion = '2024-11-05'; capabilities = @{}; clientInfo = @{ name = 'qwen-code-direct'; version = '1' } }
}
elseif ($Method -eq 'tools/list') {
    $params = @{}
}
elseif ($Method -eq 'tools/call') {
    if (-not $ToolName) { Write-Output 'ERROR: -ToolName required for tools/call'; exit 2 }
    $arguments = @{}
    if ($ArgsFile) {
        if (-not (Test-Path $ArgsFile)) { Write-Output "ERROR: args file not found: $ArgsFile"; exit 2 }
        $arguments = (Get-Content $ArgsFile -Raw | ConvertFrom-Json)
    }
    $params = @{ name = $ToolName; arguments = $arguments }
}
else { Write-Output "ERROR: unsupported method '$Method'"; exit 2 }

$body = @{ jsonrpc = '2.0'; id = (Get-Random); method = $Method; params = $params } | ConvertTo-Json -Depth 12 -Compress

$headers = @{
    'X-Goog-Api-Key' = $key
    'Content-Type'   = 'application/json'
    'Accept'         = 'application/json, text/event-stream'
}

function Scrub([string]$text) {
    if ($text -and $key) { return $text -replace [regex]::Escape($key), '[REDACTED]' }
    return $text
}

try {
    $resp = Invoke-WebRequest -Uri $Url -Method Post -Headers $headers -Body $body -TimeoutSec 180 -UseBasicParsing
    Write-Output "HTTP $($resp.StatusCode)"
    Write-Output (Scrub $resp.Content)
}
catch {
    $r = $_.Exception.Response
    if ($r) {
        Write-Output "HTTP $([int]$r.StatusCode) $($r.StatusDescription)"
        $reader = New-Object IO.StreamReader($r.GetResponseStream())
        Write-Output (Scrub $reader.ReadToEnd())
    }
    else { Write-Output ("FAILED: " + $_.Exception.Message) }
    exit 1
}
