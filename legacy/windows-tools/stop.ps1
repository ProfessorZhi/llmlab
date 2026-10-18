#Requires -Version 5.1
<#
    LLM Lab - one-click stop.

    Stops ONLY the local processes this project started, and verifies each one
    still is what its pid file claims before killing it: a recycled pid must
    never cost an unrelated process its life.

    It never touches the {{LAB_NODE}}.  Stopping the console has no effect on the
    inference node.

    ASCII only - see the note in start.ps1 about the GBK/ANSI decoding trap.

    Usage:
      powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
      powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 -WhatIfOnly
#>
[CmdletBinding()]
param(
    [switch] $WhatIfOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $Root 'run'
$Logs   = Join-Path $Root 'logs'
$CfgPath = Join-Path $Root 'config\llm-lab.toml'

# The tunnel's forwarding spec, read from config rather than duplicated here.
# If the port moves in the config and this script still looked for the old one,
# the ownership check would fail *closed* -- the tunnel would survive a stop and
# be reported as a problem, which is the safe direction, but a needless one.
$tunPort = 0
if (Test-Path $CfgPath) {
    $cfgText = Get-Content -Path $CfgPath -Raw
    if ($cfgText -match '(?ms)^\[tunnels\.strata-native\](.*?)(\r?\n\[|\Z)') {
        if ($Matches[1] -match '(?m)^\s*local_port\s*=\s*(\d+)') { $tunPort = [int]$Matches[1] }
    }
}

# pid file name -> a substring that must appear in that process's command line.
# The tunnel is matched on its `-L localhost:<port>:...` spec, not on "ssh":
# this machine runs other ssh sessions, and one of them is not ours to kill.
$Owned = @{
    'portal.pid'    = 'server.py'
    'evalscope.pid' = 'evalscope'
}
if ($tunPort -gt 0) {
    $Owned['strata-tunnel.pid'] = "localhost:$tunPort"
} else {
    $Owned['strata-tunnel.pid'] = 'id_ed25519_llmlab'
}

function Write-Line {
    param([string]$Message, [string]$Level = 'INFO')
    $color = 'Gray'
    if ($Level -eq 'WARN')  { $color = 'Yellow' }
    if ($Level -eq 'ERROR') { $color = 'Red' }
    if ($Level -eq 'OK')    { $color = 'Green' }
    Write-Host ("[{0}] {1}" -f $Level, $Message) -ForegroundColor $color
}

Write-Host ''
Write-Host '  LLM Lab - stopping local services' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path $RunDir)) {
    Write-Line "no run directory at $RunDir - nothing was started by this project" 'WARN'
    exit 0
}

$stoppedCount = 0
$problemCount = 0

foreach ($file in $Owned.Keys) {
    $pidFile = Join-Path $RunDir $file
    if (-not (Test-Path $pidFile)) {
        Write-Line "$file absent - nothing recorded for this service"
        continue
    }

    $rawPid = (Get-Content -Path $pidFile -Raw).Trim()
    $procId = 0
    if (-not [int]::TryParse($rawPid, [ref]$procId)) {
        Write-Line "$file contains '$rawPid', which is not a pid; removing the stale file" 'WARN'
        Remove-Item -Path $pidFile -Force
        continue
    }

    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        Write-Line "pid $procId ($file) is not running; removing the stale file"
        Remove-Item -Path $pidFile -Force
        continue
    }

    # Ownership check: the pid must still belong to the process we started.
    $cmdLine = ''
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
        if ($null -ne $cim) { $cmdLine = [string]$cim.CommandLine }
    } catch {
        # Get-CimInstance is not available everywhere; fall back to the path.
        $cmdLine = [string]$proc.Path
    }

    $expect = $Owned[$file]
    if ($cmdLine -notlike "*$expect*") {
        Write-Line "pid $procId is '$($proc.ProcessName)' but its command line does not contain '$expect'." 'WARN'
        Write-Line "  refusing to kill it - it is not ours. Inspect: $pidFile" 'WARN'
        Write-Line "  cmdline: $cmdLine" 'WARN'
        $problemCount++
        continue
    }

    if ($WhatIfOnly) {
        Write-Line "would stop $($proc.ProcessName) pid $procId ($file)" 'OK'
        continue
    }

    try {
        # /T so a child (a dashboard worker, a probe) does not survive its parent.
        $null = & taskkill.exe /PID $procId /T /F 2>&1
        Start-Sleep -Milliseconds 300
        if ($null -eq (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            Write-Line "stopped $($proc.ProcessName) pid $procId ($file)" 'OK'
            $stoppedCount++
        } else {
            Write-Line "pid $procId is still alive after taskkill" 'ERROR'
            $problemCount++
            continue
        }
        Remove-Item -Path $pidFile -Force
    } catch {
        Write-Line "failed to stop pid $procId : $($_.Exception.Message)" 'ERROR'
        $problemCount++
    }
}

# Anything still holding the portal port after the pid files are gone is worth
# naming, but it is NOT ours to kill - report and leave it alone.
#
# Skipped under -WhatIfOnly.  Nothing was stopped in that mode, so a listening
# port is simply the expected state, and calling it "not started from here"
# would be false: in the normal case that listener is the *child* of the pid we
# recorded, because the venv's python.exe is a launcher that spawns the
# interpreter which actually owns the socket.  stop.ps1 kills the tree with
# `taskkill /T`, so this is only a real leftover after an actual stop.
if (-not $WhatIfOnly) {
    $leftover = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
    if ($null -ne $leftover) {
        Write-Line "port 8765 is still listening (pid $($leftover.OwningProcess)) after a stop; leaving it alone." 'WARN'
    }
}

Write-Host ''
Write-Line "stopped $stoppedCount process(es); $problemCount problem(s)." $(if ($problemCount) { 'WARN' } else { 'OK' })
Write-Host '  The {{LAB_NODE}} was not touched.' -ForegroundColor DarkGray
Write-Host ''

if ($problemCount -gt 0) { exit 1 }
exit 0
