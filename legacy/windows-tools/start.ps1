#Requires -Version 5.1
<#
    LLM Lab - one-click start.

    Starts the local console, the local forwarding tunnel to the node's own UI,
    and the EvalScope dashboard, checks for port conflicts and missing
    dependencies first, then opens the homepage.  Everything it launches binds
    localhost.

    ORDERING IS THE WHOLE POINT.  The browser opens as soon as the *portal*
    answers, before the tunnel and long before EvalScope.  EvalScope's cold
    start was measured on this workstation at 31s, 242s and 459s for the same
    command (a 15x spread, driven by disk pressure), and the previous version
    waited it out -- so the homepage stayed shut for up to eight minutes.  A
    slow service must be able to be slow without holding the front door.

    It deliberately does NOT:
      * start a benchmark run,
      * touch the {{LAB_NODE}} (no restart, no config change, no file write),
      * launch anything it could not find installed.

    NOTE ON ENCODING: this file is pure ASCII on purpose.  Windows PowerShell
    5.1 reads a .ps1 without a BOM as the ANSI code page (GBK on this machine),
    which silently corrupts non-ASCII characters.  Keep it ASCII, or add a BOM.

    Usage:
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoBrowser
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoEvalScope
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoTunnel
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Force   # reuse a busy port
#>
[CmdletBinding()]
param(
    [int]    $Port,
    [switch] $NoBrowser,
    [switch] $NoEvalScope,
    [switch] $NoTunnel,
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root      = Split-Path -Parent $PSScriptRoot
$Logs      = Join-Path $Root 'logs'
$RunDir    = Join-Path $Root 'run'
$CfgPath   = Join-Path $Root 'config\llm-lab.toml'
$ServerRel = 'portal\server.py'
$ExitOk = 0

New-Item -ItemType Directory -Force -Path $Logs, $RunDir | Out-Null
$Stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
$MainLog  = Join-Path $Logs "start-$Stamp.log"

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message
    Write-Host $line
    Add-Content -Path $MainLog -Value $line -Encoding UTF8
}

function Open-Home {
    param([int]$Number)
    if ($NoBrowser) { return }
    Start-Process "http://localhost:$Number/"
    Write-Log 'opened the homepage in the default browser'
}

function Write-Banner {
    Write-Host ''
    Write-Host '  LLM Lab - Local AI Operations & Benchmark Workspace' -ForegroundColor Cyan
    Write-Host "  project : $Root"
    Write-Host "  log     : $MainLog"
    Write-Host ''
}

function Get-PortOwner {
    param([int]$Number)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction Stop |
                Select-Object -First 1
        if ($null -eq $conn) { return $null }
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($null -ne $proc) { return ("{0} (pid {1})" -f $proc.ProcessName, $proc.Id) }
        return ("pid {0}" -f $conn.OwningProcess)
    } catch { return $null }
}

function Test-HttpOk {
    param([int]$Number, [string]$Path = '/api/healthz')
    # Raw socket, not Invoke-WebRequest: a corporate proxy can intercept the
    # latter and turn a healthy loopback service into a false failure.
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('localhost', $Number)
        $stream = $client.GetStream()
        $bytes  = [Text.Encoding]::ASCII.GetBytes(
            "GET $Path HTTP/1.0`r`nHost: localhost`r`nConnection: close`r`n`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $reader = New-Object IO.StreamReader($stream)
        $resp   = $reader.ReadToEnd()
        $reader.Close(); $client.Close()
        return ($resp -match 'HTTP/1\.[01] 200')
    } catch { return $false }
}

function Invoke-PortalPost {
    param([int]$Number, [string]$Path)
    # Raw socket, for the same reason Test-HttpOk uses one: this workstation has
    # a corporate proxy, and .NET's HTTP stack can be routed through it, turning
    # a loopback call into a false failure.  Returns the parsed JSON body, or
    # $null when nothing usable came back.
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('localhost', $Number)
        $stream = $client.GetStream()
        $bytes  = [Text.Encoding]::ASCII.GetBytes(
            "POST $Path HTTP/1.0`r`nHost: localhost`r`n" +
            "X-Requested-With: llm-lab-portal`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $reader = New-Object IO.StreamReader($stream)
        $resp   = $reader.ReadToEnd()
        $reader.Close(); $client.Close()
        $split = $resp.IndexOf("`r`n`r`n")
        if ($split -lt 0) { return $null }
        $body = $resp.Substring($split + 4).Trim()
        if ([string]::IsNullOrWhiteSpace($body)) { return $null }
        return ($body | ConvertFrom-Json)
    } catch { return $null }
}

function Wait-HttpOk {
    param([int]$Number, [int]$TimeoutSec = 25, [string]$Path = '/api/healthz',
          [System.Diagnostics.Process]$Process)

    # Returns 'ok' | 'exited' | 'timeout'.
    #
    # The three outcomes are reported apart on purpose.  "No answer yet" has two
    # very different causes -- the process is still starting, or the process is
    # gone -- and a single boolean collapses them into one misleading message
    # followed by an empty stderr tail.  Observed on this workstation: a cold
    # venv interpreter on a saturated D: volume needs well over 30s just to
    # import and bind, so a small budget here reports a perfectly healthy portal
    # as a failure.  A slow start must be describable as a slow start.
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk -Number $Number -Path $Path) { return 'ok' }
        if ($null -ne $Process -and $Process.HasExited) { return 'exited' }
        Start-Sleep -Milliseconds 400
    }
    return 'timeout'
}

# ---------------------------------------------------------------- config ----
if (-not (Test-Path $CfgPath)) {
    Write-Log "config not found: $CfgPath" 'ERROR'
    exit 2
}

$cfgText    = Get-Content -Path $CfgPath -Raw
$cfgPort    = 8765
$cfgHost    = 'localhost'
$evalPort   = 9000
$waitSecs   = 120
if ($cfgText -match '(?m)^\s*port\s*=\s*(\d+)') { $cfgPort = [int]$Matches[1] }
if ($cfgText -match '(?ms)\[portal\].*?host\s*=\s*"([^"]+)"') { $cfgHost = $Matches[1] }
if ($cfgText -match '(?ms)\[services\.evalscope\].*?port\s*=\s*(\d+)') { $evalPort = [int]$Matches[1] }
if ($cfgText -match '(?ms)\[portal\].*?startup_timeout_s\s*=\s*(\d+)') { $waitSecs = [int]$Matches[1] }

# [node] -- needed for the forwarding tunnel, which is an ssh session run from
# this machine.  Parsed per-section (up to the next [...] header) rather than
# with one greedy pattern, so that adding a key elsewhere cannot silently
# change which value this picks up.
$nodeHost = ''
$sshUser = ''
$sshKey = ''
$sshKnownHosts = ''
$sshTimeout = 20
if ($cfgText -match '(?ms)^\[node\](.*?)(\r?\n\[|\Z)') {
    $nodeBlock = $Matches[1]
    if ($nodeBlock -match '(?m)^\s*host\s*=\s*"([^"]+)"')          { $nodeHost = $Matches[1] }
    if ($nodeBlock -match '(?m)^\s*ssh_user\s*=\s*"([^"]+)"')      { $sshUser = $Matches[1] }
    if ($nodeBlock -match '(?m)^\s*ssh_key\s*=\s*"([^"]+)"')       { $sshKey = $Matches[1] }
    if ($nodeBlock -match '(?m)^\s*ssh_known_hosts\s*=\s*"([^"]+)"') { $sshKnownHosts = $Matches[1] }
    if ($nodeBlock -match '(?m)^\s*ssh_timeout_s\s*=\s*(\d+)')     { $sshTimeout = [int]$Matches[1] }
}

# [tunnels.strata-native] -- the local forwarding port for the node's own UI.
$tunEnabled = $false
$tunPort = 0
$tunRemoteHost = 'localhost'
$tunRemotePort = 8080
if ($cfgText -match '(?ms)^\[tunnels\.strata-native\](.*?)(\r?\n\[|\Z)') {
    $tunBlock = $Matches[1]
    if ($tunBlock -match '(?m)^\s*enabled\s*=\s*(true|false)')     { $tunEnabled = ($Matches[1] -eq 'true') }
    if ($tunBlock -match '(?m)^\s*local_port\s*=\s*(\d+)')         { $tunPort = [int]$Matches[1] }
    if ($tunBlock -match '(?m)^\s*remote_host\s*=\s*"([^"]+)"')    { $tunRemoteHost = $Matches[1] }
    if ($tunBlock -match '(?m)^\s*remote_port\s*=\s*(\d+)')        { $tunRemotePort = [int]$Matches[1] }
}

if ($Port -le 0) { $Port = $cfgPort }

Write-Banner
Write-Log "config parsed: portal host=$cfgHost port=$Port (budget ${waitSecs}s), evalscope port=$evalPort"
if ($tunEnabled) {
    Write-Log ("tunnel configured: localhost:{0} -> {1}@{2}:{3}:{4}" -f `
        $tunPort, $sshUser, $nodeHost, $tunRemoteHost, $tunRemotePort)
} else {
    Write-Log 'tunnel: disabled in config'
}

if ($cfgHost -ne 'localhost' -and $cfgHost -ne 'localhost') {
    Write-Log "REFUSING: config binds '$cfgHost'. This console has no authentication;" 'ERROR'
    Write-Log "the brief requires localhost. Fix [portal].host in config/llm-lab.example.toml." 'ERROR'
    exit 2
}

# ---------------------------------------------------------------- python ----
$Python   = $null
$PyPrefix = @()

$candidates = @(
    (Join-Path $Root 'evaluation\.venv\Scripts\python.exe'),
    (Join-Path $Root '.venv\Scripts\python.exe')
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $Python = $c; Write-Log "python: project venv ($c)"; break }
}
if ($null -eq $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $Python = $cmd.Source; Write-Log "python: $($cmd.Source) (system)" }
}
if ($null -eq $Python) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $Python = $cmd.Source; $PyPrefix = @('-3'); Write-Log "python: py launcher" }
}
if ($null -eq $Python) {
    Write-Log 'no Python found on PATH and no project venv. Install Python 3.11+.' 'ERROR'
    exit 2
}

try {
    $verText = (& $Python @PyPrefix -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>&1 | Select-Object -Last 1)
    Write-Log "python version: $verText"
    $parts = "$verText".Trim().Split('.')
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
        Write-Log 'Python 3.11+ is required (the portal reads its config with tomllib).' 'ERROR'
        exit 2
    }
} catch {
    Write-Log "python is not runnable: $($_.Exception.Message)" 'ERROR'
    exit 2
}

# ------------------------------------------------------------ port check ----
# A healthy portal on the port is not a reason to exit: start.ps1 means "bring
# the lab up", so it still has the EvalScope half of its job to do.  It used to
# exit 0 here, which silently skipped the dashboard whenever the portal happened
# to be already running.
$portalReused = $false
$busy = Get-PortOwner -Number $Port
if ($null -ne $busy) {
    if (Test-HttpOk -Number $Port) {
        Write-Log "port $Port already answers /api/healthz - a portal is running; reusing it."
        $portalReused = $true
    } else {
        if (-not $Force) {
            Write-Log "port $Port is held by $busy and is not a healthy portal." 'ERROR'
            Write-Log 'Refusing to steal it. Free the port, or pass -Force to pick another one.' 'ERROR'
            exit 3
        }
        $Port = $Port + 1
        while ($null -ne (Get-PortOwner -Number $Port)) { $Port = $Port + 1 }
        Write-Log "-Force: moved to free port $Port"
    }
}

# ---------------------------------------------------------------- launch ----
$started = New-Object System.Collections.ArrayList

function Get-PidFileValue {
    param([string]$Path)
    # Returns an int, or $null when the file is missing, empty or not a number.
    # Written defensively because Set-StrictMode turns an accidental method call
    # on $null into a terminating error.
    if (-not (Test-Path $Path)) { return $null }
    $text = ''
    try { $text = (Get-Content -Path $Path -Raw -ErrorAction Stop) } catch { return $null }
    if ($null -eq $text) { return $null }
    $text = $text.Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    $n = 0
    if ([int]::TryParse($text, [ref]$n)) { return $n }
    return $null
}

if ($portalReused) {
    [void]$started.Add([pscustomobject]@{
        Service = 'LLM Lab portal (already running)'
        Url     = "http://localhost:$Port/"
        Pid     = (Get-PidFileValue -Path (Join-Path $RunDir 'portal.pid'))
    })
} else {
    $outLog = Join-Path $Logs "portal-$Stamp.out.log"
    $errLog = Join-Path $Logs "portal-$Stamp.err.log"
    Write-Log "launching portal: $Python $(($PyPrefix + @('-u', $ServerRel)) -join ' ')"

    try {
        $portal = Start-Process -FilePath $Python `
            -ArgumentList ($PyPrefix + @('-u', $ServerRel)) `
            -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } catch {
        Write-Log "failed to launch the portal: $($_.Exception.Message)" 'ERROR'
        exit 4
    }

    Set-Content -Path (Join-Path $RunDir 'portal.pid') -Value $portal.Id -Encoding ASCII
    Write-Log "portal pid $($portal.Id); stdout -> $outLog; stderr -> $errLog"

    $ready = Wait-HttpOk -Number $Port -TimeoutSec $waitSecs -Process $portal
    if ($ready -ne 'ok') {
        if ($ready -eq 'exited') {
            Write-Log "the portal process exited during startup (code $($portal.ExitCode))." 'ERROR'
        } else {
            Write-Log "the portal process is still alive after ${waitSecs}s but has not answered on localhost:$Port." 'ERROR'
            Write-Log 'It is still starting, not dead. A cold interpreter on a busy disk can take minutes;' 'ERROR'
            Write-Log 'raise [portal].startup_timeout_s in the config, or wait and retry the port once.' 'ERROR'
        }
        if (Test-Path $errLog) {
            $errText = (Get-Content -Path $errLog -Raw -ErrorAction SilentlyContinue)
            if ([string]::IsNullOrWhiteSpace($errText)) {
                Write-Log "stderr is empty - the process wrote nothing, which points at a slow start" 'ERROR'
                Write-Log "rather than a crash. See $outLog for its own progress output." 'ERROR'
            } else {
                Write-Log 'last stderr lines:' 'ERROR'
                Get-Content -Path $errLog -Tail 25 | ForEach-Object { Write-Log "  $_" 'ERROR' }
            }
        }
        exit 4
    }
    Write-Log "portal healthy at http://localhost:$Port/ (answered within ${waitSecs}s budget)"

    # The venv's Scripts\python.exe is a uv trampoline: it spawns the real
    # interpreter, and it is the child that owns the socket (observed here:
    # pid 13596 parent / pid 6940 listener, created the same second).  The
    # trampoline is the right pid to record, because stop.ps1 uses
    # `taskkill /T` on it and that takes the whole tree down.  It is only the
    # right pid while it is alive, though: if a launcher ever exits after
    # spawning, the pid file would go stale while the portal kept serving, and
    # stop.ps1 would delete the file as stale and leave the portal running.
    # So fall back to the socket owner in that case.
    if ($portal.HasExited) {
        $sock = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if ($null -ne $sock) {
            Set-Content -Path (Join-Path $RunDir 'portal.pid') -Value $sock.OwningProcess -Encoding ASCII
            Write-Log "launcher pid $($portal.Id) exited; recorded socket owner pid $($sock.OwningProcess) instead"
        }
    }

    [void]$started.Add([pscustomobject]@{
        Service = 'LLM Lab portal'; Url = "http://localhost:$Port/"; Pid = $portal.Id
    })
}

# The homepage is worth opening now, not at the end.  Everything below this
# point is addition, and none of it should be able to delay the front door.
Open-Home -Number $Port

# ------------------------------------------------------- native UI tunnel ----
# A forwarded loopback port is a stable entry to the node's own console; the
# node's LAN address is not (it changes when this workstation moves networks).
# `-N` means no remote command, so this is a pure forward.  The ssh process is
# deliberately *not* waited on beyond a short probe: a tunnel that is slow to
# come up must not hold the start, and one that fails to bind exits on its own
# because of ExitOnForwardFailure, so a stale listener can never be mistaken
# for a working tunnel.
if ($NoTunnel) {
    Write-Log 'tunnel: skipped (-NoTunnel)'
} elseif (-not $tunEnabled) {
    Write-Log 'tunnel: disabled in config'
} elseif ($tunPort -le 0 -or [string]::IsNullOrWhiteSpace($nodeHost) -or [string]::IsNullOrWhiteSpace($sshUser)) {
    Write-Log 'tunnel: config incomplete (need local_port, [node].host and [node].ssh_user) - skipped' 'WARN'
} else {
    $tunPidFile = Join-Path $RunDir 'strata-tunnel.pid'
    $tunBusy = Get-PortOwner -Number $tunPort

    if ($null -ne $tunBusy) {
        # Something is listening.  Whether it is *our* tunnel is a separate
        # question that a port check cannot answer -- on this workstation port
        # 18080 is held by an unrelated service that answers HTTP 200 on every
        # path, so "the port answers" is not evidence of anything.  Say the
        # ambiguity instead of resolving it wrongly.
        $conflict = "localhost:$tunPort is already held by $tunBusy"
        if (Test-Path $tunPidFile) {
            Write-Log "tunnel: $conflict; reusing it (run\strata-tunnel.pid exists)"
            [void]$started.Add([pscustomobject]@{
                Service = 'Strata native UI tunnel (pre-existing)'
                Url     = "http://localhost:$tunPort/"
                Pid     = (Get-PidFileValue -Path $tunPidFile)
            })
        } else {
            Write-Log "tunnel: $conflict and no pid file from us - NOT touching it." 'WARN'
            Write-Log "the node's own UI is still reachable at http://${nodeHost}:$tunRemotePort" 'WARN'
        }
    } else {
        $sshExe = $null
        foreach ($name in @('ssh.exe', 'ssh')) {
            $found = Get-Command $name -ErrorAction SilentlyContinue
            if ($null -ne $found) { $sshExe = $found.Source; break }
        }
        $keyPath = Join-Path $Root $sshKey
        $khPath  = Join-Path $Root $sshKnownHosts

        if ($null -eq $sshExe) {
            Write-Log 'tunnel: no ssh client found on PATH - skipped' 'WARN'
        } elseif (-not (Test-Path $keyPath)) {
            Write-Log "tunnel: private key missing: $keyPath" 'WARN'
        } else {
            # The bare "-L a:b:c" form is avoided: in PowerShell a colon right
            # after a variable name ($tunPort:) parses as a drive qualifier, so
            # the string is built with -f.
            $fwd = "localhost:{0}:{1}:{2}" -f $tunPort, $tunRemoteHost, $tunRemotePort
            $sshArgs = @(
                '-N',
                '-o', 'ExitOnForwardFailure=yes',
                '-o', 'BatchMode=yes',
                '-o', 'IdentitiesOnly=yes',
                '-o', 'StrictHostKeyChecking=yes',
                '-o', "UserKnownHostsFile=$khPath",
                '-o', "ConnectTimeout=$sshTimeout",
                '-o', 'ServerAliveInterval=30',
                '-o', 'ServerAliveCountMax=3',
                '-i', $keyPath,
                '-L', $fwd,
                "$sshUser@$nodeHost"
            )
            $tOut = Join-Path $Logs "tunnel-$Stamp.out.log"
            $tErr = Join-Path $Logs "tunnel-$Stamp.err.log"
            try {
                $tun = Start-Process -FilePath $sshExe -ArgumentList $sshArgs `
                    -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
                    -RedirectStandardOutput $tOut -RedirectStandardError $tErr
                Set-Content -Path $tunPidFile -Value $tun.Id -Encoding ASCII

                $tunOk = $false
                $tunDeadline = (Get-Date).AddSeconds(10)
                while ((Get-Date) -lt $tunDeadline) {
                    if ($tun.HasExited) { break }
                    if ($null -ne (Get-PortOwner -Number $tunPort)) { $tunOk = $true; break }
                    Start-Sleep -Milliseconds 300
                }

                if ($tunOk) {
                    Write-Log "tunnel up: http://localhost:$tunPort/ -> $nodeHost`:$tunRemotePort (pid $($tun.Id))"
                    [void]$started.Add([pscustomobject]@{
                        Service = 'Strata native UI tunnel'
                        Url     = "http://localhost:$tunPort/"; Pid = $tun.Id
                    })
                } else {
                    # A fail here is informative, not fatal: the node's own port
                    # is still directly reachable, and the portal reports both
                    # routes separately so a dead tunnel never reads as a dead
                    # node.
                    Write-Log "tunnel did not come up within 10s (pid $($tun.Id) exited=$($tun.HasExited))" 'WARN'
                    if (Test-Path $tErr) {
                        $tErrText = (Get-Content -Path $tErr -Raw -ErrorAction SilentlyContinue)
                        if (-not [string]::IsNullOrWhiteSpace($tErrText)) {
                            Get-Content -Path $tErr -Tail 6 | ForEach-Object { Write-Log "  $_" 'WARN' }
                        }
                    }
                    Write-Log "the node's own UI is still reachable at http://${nodeHost}:$tunRemotePort" 'WARN'
                }
            } catch {
                Write-Log "tunnel failed to launch: $($_.Exception.Message)" 'WARN'
            }
        }
    }
}

# ------------------------------------------------------------- evalscope ----
# EvalScope is launched by the *portal*, over HTTP, rather than here.  Two
# reasons, and the second is the one that matters:
#
#   1. One launcher, one command line.  The argv lives in the config file
#      ([services.evalscope].launch) and the portal substitutes the port.  A
#      second copy of the same argv in this script is a copy that drifts, and
#      the drift shows up as "the button works but start.ps1 does not".
#   2. Nothing is waited on.  The portal returns as soon as the process is
#      spawned; this script does not poll, so EvalScope's 31s-459s cold start
#      cannot delay anything.  The homepage already opened, and it shows the
#      service as "starting (pid N alive, not bound yet)" until it binds --
#      which is a state the old blocking version could not express at all.
if ($NoEvalScope) {
    Write-Log 'EvalScope: skipped (-NoEvalScope)'
} elseif (-not (Test-HttpOk -Number $Port)) {
    Write-Log 'EvalScope: the portal is not answering - use the homepage button' 'WARN'
} else {
    $res = Invoke-PortalPost -Number $Port -Path '/api/service/start?key=evalscope'
    if ($null -eq $res) {
        Write-Log 'EvalScope: no usable reply from /api/service/start - use the homepage button' 'WARN'
    } elseif ($res.ok -and $res.started) {
        Write-Log "EvalScope: launched by the portal (pid $($res.pid)); NOT waiting - it imports for minutes and the homepage already reports it as starting"
        [void]$started.Add([pscustomobject]@{
            Service = 'EvalScope dashboard (starting)'
            Url     = "http://localhost:$evalPort/dashboard"; Pid = $res.pid
        })
    } elseif ($res.ok) {
        Write-Log "EvalScope: $($res.note)"
        [void]$started.Add([pscustomobject]@{
            Service = "EvalScope dashboard ($($res.state))"
            Url     = "http://localhost:$evalPort/dashboard"; Pid = $res.pid
        })
    } else {
        Write-Log "EvalScope: not started - $($res.error)" 'WARN'
    }
}

# --------------------------------------------------------------- summary ----
Write-Host ''
Write-Host '  Started:' -ForegroundColor Green
$started | Format-Table -AutoSize | Out-String | Write-Host
Write-Host '  Loopback only. Stopping this console does not affect the node.' -ForegroundColor DarkGray
Write-Host "  Stop with: powershell -ExecutionPolicy Bypass -File scripts\stop.ps1"
Write-Host ''

exit $ExitOk
