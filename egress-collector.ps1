# Egress monitor: records WHICH program connects to WHICH AI provider, and for how long.
# It never reads packet contents, prompts, responses or command lines, and it needs no certificate.
# Output (in the project folder):  egress_log.jsonl  (one line per finished connection)
#                                  egress_active.json (live snapshot + heartbeat, rewritten every poll)
# Limits: a connection being open is not proof data was sent; connections shorter than the poll
# interval can be missed; apps using DNS-over-HTTPS are recognised only through probe_hosts.
param([int]$PollSeconds = 3, [switch]$Once)
$ErrorActionPreference = 'Continue'
$Root       = 'C:\dev\openclaw-router'
$LogFile    = Join-Path $Root 'egress_log.jsonl'
$ActiveFile = Join-Path $Root 'egress_active.json'
$InLogFile  = Join-Path $Root 'ingress_log.jsonl'
$RouterPort = 6060
$DomainFile = Join-Path $Root 'egress_domains.json'
$MaxLogBytes = 5MB

$mutex = New-Object System.Threading.Mutex($false, 'Local\ModelRouterEgress')
if (-not $Once -and -not $mutex.WaitOne(0)) { exit 0 }

$script:Flat = @(); $script:Probe = @()
$script:IpMap = @{}; $script:IpNames = @{}; $script:Names = @{}; $script:Paths = @{}; $script:Blocks = @()
function Iso([DateTime]$d) { return $d.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

function Load-Domains {
    try {
        $cfg = Get-Content -Raw -Path $DomainFile -ErrorAction Stop | ConvertFrom-Json
        $flat = @()
        foreach ($p in $cfg.providers.PSObject.Properties) {
            foreach ($s in @($p.Value)) { $flat += [pscustomobject]@{ s = ([string]$s).ToLower(); p = $p.Name } }
        }
        $script:Flat  = @($flat | Sort-Object { $_.s.Length } -Descending)
        $script:Probe = @($cfg.probe_hosts)
    } catch { Write-Host "could not read egress_domains.json: $($_.Exception.Message)" }
}

function Provider-Of([string]$name) {
    if (-not $name) { return $null }
    $n = $name.TrimEnd('.').ToLower()
    foreach ($d in $script:Flat) { if ($n -eq $d.s -or $n.EndsWith('.' + $d.s)) { return $d.p } }
    return $null
}

function Remember-Ip([string]$ip, [string]$name) {
    if (-not $ip -or -not $name) { return }
    if (-not $script:IpNames.ContainsKey($ip)) { $script:IpNames[$ip] = @{} }
    if ($script:IpNames[$ip].Count -lt 12) { $script:IpNames[$ip][$name.TrimEnd('.').ToLower()] = 1 }
    if ($script:IpMap.ContainsKey($ip) -and (Provider-Of $script:IpMap[$ip]) -and -not (Provider-Of $name)) { return }
    $script:IpMap[$ip] = $name
}

function Update-FromDnsCache {
    try { $entries = Get-DnsClientCache -ErrorAction Stop } catch { return }
    foreach ($e in $entries) {
        if ($e.Data -and ($e.Type -eq 1 -or $e.Type -eq 28)) {
            $name = $e.Entry
            if (-not (Provider-Of $name) -and (Provider-Of $e.Name)) { $name = $e.Name }
            Remember-Ip ([string]$e.Data) $name
        }
    }
}

function Update-FromProbe {
    foreach ($h in $script:Probe) {
        foreach ($t in 'A', 'AAAA') {
            try { $r = Resolve-DnsName -Name $h -Type $t -DnsOnly -ErrorAction Stop } catch { continue }
            foreach ($a in $r) { if ($a.IPAddress) { Remember-Ip ([string]$a.IPAddress) $h } }
        }
    }
}

# An address that has also been resolved for a non-AI site (typical of Cloudflare/CDN addresses), or for
# more than one AI provider, cannot be pinned to a single service from the address alone.
function Is-Shared([string]$ip) {
    if (-not $script:IpNames.ContainsKey($ip)) { return $false }
    $provs = @{}
    foreach ($n in $script:IpNames[$ip].Keys) {
        $p = Provider-Of $n
        if (-not $p) { return $true }
        $provs[$p] = 1
    }
    return ($provs.Count -gt 1)
}

function Is-Private([string]$ip) {
    return $ip -match '^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|0\.0\.0\.0|::1$|::$|fe80|fc|fd)'
}

function Remote-Kind([string]$ip) {
    if ($ip -match '^(127\.|::1$)') { return 'loopback' }
    if ($ip -match '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.') { return 'tailscale' }
    if ($ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|fe80|fc|fd)') { return 'lan' }
    return 'public'
}

function Proc-Name([int]$procId) {
    if ($script:Names.ContainsKey($procId)) { return $script:Names[$procId] }
    $n = $null; $path = $null
    try { $gp = Get-Process -Id $procId -ErrorAction Stop; $n = $gp.ProcessName; $path = $gp.Path } catch {}
    if (-not $n) { $n = "pid$procId" }
    $script:Names[$procId] = $n; $script:Paths[$procId] = [string]$path
    return $n
}

# Firewall rules this tool created (read-only listing; creating or removing them needs admin and is done by
# the scripts the Control Center generates, never by this collector).
function Update-Blocks {
    $out = @()
    try {
        foreach ($r in @(Get-NetFirewallRule -DisplayName 'ModelRouter-Block-*' -ErrorAction Stop)) {
            $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $r -ErrorAction SilentlyContinue
            $adr = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $r -ErrorAction SilentlyContinue
            $out += [ordered]@{ name = [string]$r.DisplayName; enabled = ([string]$r.Enabled -eq 'True'); program = [string]$app.Program
                                remote = @($adr.RemoteAddress | ForEach-Object { [string]$_ } | Select-Object -First 20) }
        }
    } catch {}
    $script:Blocks = $out
}

function Append-Log([string]$line, [string]$file = $LogFile) {
    try {
        if ((Test-Path $file) -and (Get-Item $file).Length -gt $MaxLogBytes) { Move-Item -Force $file ($file + '.1') }
        [System.IO.File]::AppendAllText($file, $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch { Write-Host "log write failed: $($_.Exception.Message)" }
}

function Write-Closed($c) {
    $secs = [math]::Max(0, [int](($c.last - $c.start).TotalSeconds))
    $o = [ordered]@{ v = 1; start = (Iso $c.start); end = (Iso $c.last); seconds = $secs; proc = $c.proc
                     provider = $c.provider; domain = $c.domain; ip = $c.ip; port = [int]$c.port; router = [bool]$c.router; shared = [bool]$c.shared; pid = [int]$c.pid; path = [string]$c.path }
    Append-Log ($o | ConvertTo-Json -Compress)
}

function Write-InClosed($c) {
    $secs = [math]::Max(0, [int](($c.last - $c.start).TotalSeconds))
    $o = [ordered]@{ v = 1; type = $c.type; start = (Iso $c.start); end = (Iso $c.last); seconds = $secs; proc = $c.proc; pid = [int]$c.pid
                     port = [int]$c.port; remote = $c.remote; kind = $c.kind }
    Append-Log ($o | ConvertTo-Json -Compress) $InLogFile
}

Load-Domains
$active = @{}; $inActive = @{}
$lastCache = [DateTime]::MinValue; $lastProbe = [DateTime]::MinValue; $lastNames = [DateTime]::UtcNow; $lastBlocks = [DateTime]::MinValue

try {
    while ($true) {
        $now = [DateTime]::UtcNow
        if (($now - $lastCache).TotalSeconds -ge 20)  { Update-FromDnsCache; $lastCache = $now }
        if (($now - $lastProbe).TotalSeconds -ge 300) { Load-Domains; Update-FromProbe; Update-FromDnsCache; $lastProbe = $now }
        if (($now - $lastNames).TotalMinutes -ge 10)  { $script:Names = @{}; $script:Paths = @{}; $lastNames = $now }
        if (($now - $lastBlocks).TotalSeconds -ge 30) { Update-Blocks; $lastBlocks = $now }

        $listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
        $routerPids = @($listen | Where-Object { $_.LocalPort -eq $RouterPort } | Select-Object -ExpandProperty OwningProcess -Unique)
        $listenPorts = @{}; $listeners = @{}
        foreach ($l in $listen) {
            $listenPorts[[int]$l.LocalPort] = 1
            $addr = [string]$l.LocalAddress
            $scope = if ($addr -match '^(127\.|::1$)') { 'local' } elseif ($addr -eq '0.0.0.0' -or $addr -eq '::') { 'all-interfaces' } else { 'network' }
            $lname = Proc-Name $l.OwningProcess
            $lk = '{0}|{1}|{2}' -f $lname, $l.LocalPort, $scope
            if (-not $listeners.ContainsKey($lk) -and $listeners.Count -lt 200) {
                $listeners[$lk] = [ordered]@{ proc = $lname; pid = [int]$l.OwningProcess; port = [int]$l.LocalPort; scope = $scope; address = $addr; router = ($routerPids -contains $l.OwningProcess) }
            }
        }
        $seen = @{}; $unres = @{}; $inSeen = @{}
        foreach ($c in @(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue)) {
            $ip = [string]$c.RemoteAddress
            # Inbound: someone connected to a port this machine listens on (server side of the connection).
            if ($listenPorts.ContainsKey([int]$c.LocalPort)) {
                $rk = Remote-Kind $ip
                if ($rk -ne 'loopback') {
                    $ik = 'in|{0}|{1}|{2}|{3}' -f $c.OwningProcess, $c.LocalPort, $ip, $c.RemotePort
                    $inSeen[$ik] = 1
                    if (-not $inActive.ContainsKey($ik) -and $inActive.Count -lt 500) {
                        $inActive[$ik] = @{ type = 'remote'; start = $now; last = $now; proc = (Proc-Name $c.OwningProcess); pid = [int]$c.OwningProcess; port = [int]$c.LocalPort; remote = $ip; kind = $rk }
                    }
                    if ($inActive.ContainsKey($ik)) { $inActive[$ik].last = $now }
                }
            }
            # Which local program is calling the router: the client side of a loopback connection to its port.
            if ([int]$c.RemotePort -eq $RouterPort -and $ip -match '^(127\.|::1$)' -and [int]$c.LocalPort -ne $RouterPort) {
                $ik = 'rc|{0}|{1}' -f $c.OwningProcess, $c.LocalPort
                $inSeen[$ik] = 1
                if (-not $inActive.ContainsKey($ik) -and $inActive.Count -lt 500) {
                    $inActive[$ik] = @{ type = 'router-client'; start = $now; last = $now; proc = (Proc-Name $c.OwningProcess); pid = [int]$c.OwningProcess; port = [int]$RouterPort; remote = $ip; kind = 'loopback' }
                }
                if ($inActive.ContainsKey($ik)) { $inActive[$ik].last = $now }
            }
            if (Is-Private $ip) { continue }
            $domain = $script:IpMap[$ip]
            $prov = Provider-Of $domain
            $name = Proc-Name $c.OwningProcess
            if ($prov) {
                $key = '{0}|{1}|{2}|{3}' -f $c.OwningProcess, $ip, $c.RemotePort, $c.LocalPort
                $seen[$key] = 1
                if (-not $active.ContainsKey($key)) {
                    $active[$key] = @{ start = $now; last = $now; proc = $name; provider = $prov; domain = $domain
                                       ip = $ip; port = $c.RemotePort; router = ($routerPids -contains $c.OwningProcess); shared = $false; pid = [int]$c.OwningProcess; path = [string]$script:Paths[[int]$c.OwningProcess] }
                }
                $active[$key].last = $now
                if (-not $active[$key].shared -and (Is-Shared $ip)) { $active[$key].shared = $true }
            } elseif ($c.RemotePort -eq 443) {
                if (-not $unres.ContainsKey($name)) { $unres[$name] = New-Object System.Collections.ArrayList }
                if ($unres[$name] -notcontains $ip) { [void]$unres[$name].Add($ip) }
            }
        }
        foreach ($k in @($active.Keys)) {
            if (-not $seen.ContainsKey($k)) { Write-Closed $active[$k]; $active.Remove($k) }
        }

        foreach ($k in @($inActive.Keys)) {
            if (-not $inSeen.ContainsKey($k)) { Write-InClosed $inActive[$k]; $inActive.Remove($k) }
        }
        $inList = @(); foreach ($a in $inActive.Values) {
            $inList += [ordered]@{ type = $a.type; start = (Iso $a.start); seconds = [int](($now - $a.start).TotalSeconds); proc = $a.proc; pid = [int]$a.pid
                                   port = [int]$a.port; remote = $a.remote; kind = $a.kind }
        }
        $list = @(); foreach ($a in $active.Values) {
            $list += [ordered]@{ start = (Iso $a.start); seconds = [int](($now - $a.start).TotalSeconds); proc = $a.proc; provider = $a.provider
                                 domain = $a.domain; ip = $a.ip; port = [int]$a.port; router = [bool]$a.router; shared = [bool]$a.shared; pid = [int]$a.pid; path = [string]$a.path }
        }
        $ulist = @(); foreach ($n in $unres.Keys) {
            $ulist += [ordered]@{ proc = $n; connections = $unres[$n].Count; sample_ips = @($unres[$n] | Select-Object -First 3) }
        }
        $ulist = @($ulist | Sort-Object { $_.connections } -Descending | Select-Object -First 20)
        $snap = [ordered]@{ at = (Iso $now); poll_seconds = $PollSeconds; collector_pid = $PID; domains = $script:Flat.Count
                            ip_map = $script:IpMap.Count; router_pids = @($routerPids); blocks = @($script:Blocks); router_port = $RouterPort; listeners = @($listeners.Values); inbound = @($inList | Select-Object -First 200); active = $list; unresolved = $ulist }
        try {
            $tmp = $ActiveFile + '.tmp'
            [System.IO.File]::WriteAllText($tmp, ($snap | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -Force $tmp $ActiveFile
        } catch { Write-Host "snapshot write failed: $($_.Exception.Message)" }

        if ($Once) { Write-Host ("active AI connections: {0}; unidentified 443 processes: {1}; ip map: {2}" -f $list.Count, $ulist.Count, $script:IpMap.Count); $list | ForEach-Object { "{0,-18} {1,-14} {2}" -f $_.proc, $_.provider, $_.domain }; break }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    foreach ($k in @($active.Keys)) { Write-Closed $active[$k] }
    foreach ($k in @($inActive.Keys)) { Write-InClosed $inActive[$k] }
    if (-not $Once) { try { $mutex.ReleaseMutex() } catch {} }
}
