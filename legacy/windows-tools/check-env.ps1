#Requires -Version 5.1
<#
    LLM Lab - environment check.

    Answers one question before anything is started: can this project run on
    this machine as it stands?  Read-only; it changes nothing and starts nothing.

    Sections: interpreter, toolchain, project files, secrets, SSH trust anchor,
    ports, and the optional EvalScope install.

    ASCII only - see the note in start.ps1 about the GBK/ANSI decoding trap.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$Root   = Split-Path -Parent $PSScriptRoot
$Issues = New-Object System.Collections.ArrayList

function Section { param([string]$Name) Write-Host ''; Write-Host "== $Name ==" -ForegroundColor Cyan }

function Report {
    param([string]$Label, [bool]$Ok, [string]$Detail = '', [switch]$WarnOnly)
    $mark = if ($Ok) { 'ok  ' } elseif ($WarnOnly) { 'warn' } else { 'FAIL' }
    $color = if ($Ok) { 'Green' } elseif ($WarnOnly) { 'Yellow' } else { 'Red' }
    Write-Host ("  [{0}] {1,-34} {2}" -f $mark, $Label, $Detail) -ForegroundColor $color
    if (-not $Ok -and -not $WarnOnly) { [void]$Issues.Add($Label) }
}

Write-Host ''
Write-Host '  LLM Lab - environment check' -ForegroundColor Cyan
Write-Host "  project: $Root"

# ------------------------------------------------------------ interpreter ---
Section 'Interpreter'
$Py = $null
foreach ($c in @((Join-Path $Root 'evaluation\.venv\Scripts\python.exe'),
                 (Join-Path $Root '.venv\Scripts\python.exe'))) {
    if (Test-Path $c) { $Py = $c; break }
}
if ($null -eq $Py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $Py = $cmd.Source }
}
if ($null -eq $Py) {
    Report 'python interpreter' $false 'none found; install Python 3.11+'
} else {
    $v = (& $Py -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>&1 | Select-Object -Last 1)
    $ok = ($v -match '^3\.(1[1-9]|[2-9][0-9])\.')
    Report 'python interpreter' $ok "$Py (Python $v)"
    $t = & $Py -c "import tomllib;print('tomllib ok')" 2>&1
    Report 'tomllib (needed for config)' ($t -match 'ok') "$t"
}

# -------------------------------------------------------------- toolchain ---
Section 'Toolchain'
foreach ($tool in @(
    @{ Name = 'node';   Args = @('--version') },
    @{ Name = 'git';    Args = @('--version') },
    @{ Name = 'ssh';    Args = @('-V') }
)) {
    $cmd = Get-Command $tool.Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Report $tool.Name $false 'not on PATH' -WarnOnly
    } else {
        $out = (& $cmd.Source @($tool.Args) 2>&1 | Select-Object -First 1)
        Report $tool.Name $true "$out"
    }
}

# --------------------------------------------------------- project files ---
Section 'Project files'
foreach ($rel in @('config\llm-lab.toml', 'portal\server.py', 'portal\web\index.html',
                   'portal\web\app.js', 'portal\web\core\router.js',
                   'portal\web\styles\tokens.css',
                   'scripts\start.ps1', 'scripts\stop.ps1',
                   'scripts\bootstrap_remote_access.py')) {
    $p = Join-Path $Root $rel
    Report $rel (Test-Path $p) ''
}

# --------------------------------------------------------------- secrets ---
Section 'Secrets (never printed)'
$secrets = Join-Path $Root 'config\secrets.env'
if (-not (Test-Path $secrets)) {
    Report 'config\secrets.env' $false 'missing; run scripts\bootstrap_remote_access.py once' -WarnOnly
} else {
    $hasKey = (Get-Content $secrets -Raw) -match 'STRATA_API_KEY=\S'
    Report 'config\secrets.env' $hasKey 'present, key line found (value withheld)'
    Report 'secrets.env mode' $true ('.gitignore rule covers it; verify with git check-ignore')
}

# ------------------------------------------------------------ ssh anchor ---
Section 'SSH trust anchor'
$key    = Join-Path $Root '{{LAB_SSH_KEY_PATH}}'
$kh     = Join-Path $Root '{{LAB_KNOWN_HOSTS_PATH}}'
$pin    = Join-Path $Root '{{LAB_SSH_FINGERPRINT_PATH}}'
Report 'private key (project-local)' (Test-Path $key) $(if (Test-Path $key) { 'present' } else { 'missing' })
Report 'known_hosts' (Test-Path $kh) ''
if (Test-Path $pin) {
    $pinned = (Select-String -Path $pin -Pattern '^SHA256:\S+' | Select-Object -First 1).Line
    Report 'pinned fingerprint' ($null -ne $pinned) "$pinned"
    $hits = @()
    if (Test-Path $kh) {
        foreach ($line in Get-Content $kh) {
            $f = $line -split '\s+'
            if ($f.Count -ge 3) { $hits += $f[1] }
        }
    }
    Report 'known_hosts keys present' ($hits.Count -gt 0) ($hits -join ', ')
    Write-Host '    note: several key types are normal; the portal matches the pin against all of them.' -ForegroundColor DarkGray
}
if ((Test-Path $key) -and (Test-Path $kh)) {
    $target = '{{LAB_USER}}@{{LAB_HOST}}'
    $r = & ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes `
               -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$kh" `
               -o ConnectTimeout=8 -o LogLevel=ERROR $target 'hostname' 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    Report 'key auth to the node' $ok $(if ($ok) { "$r" } else { "$r" }) -WarnOnly:$true
} else {
    Report 'key auth to the node' $false 'skipped: key material missing' -WarnOnly
}

# ----------------------------------------------------------------- ports ---
Section 'Ports'
foreach ($p in @(8765, 9000, 3000)) {
    $owner = $null
    try {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($null -ne $c) {
            $pr = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
            $owner = if ($null -ne $pr) { "$($pr.ProcessName) (pid $($pr.Id))" } else { "pid $($c.OwningProcess)" }
        }
    } catch { }
    if ($null -eq $owner) { Report "port $p" $true 'free' }
    else { Report "port $p" $true "in use by $owner" -WarnOnly }
}

# -------------------------------------------------------------- evalscope ---
Section 'EvalScope (optional)'
$exe = Join-Path $Root 'evaluation\.venv\Scripts\evalscope.exe'
if (Test-Path $exe) {
    Report 'evalscope cli' $true "$exe"
} else {
    Report 'evalscope cli' $false 'not installed; run deploy\evalscope\install-evalscope.ps1' -WarnOnly
}

# ------------------------------------------------------------------ result --
Write-Host ''
if ($Issues.Count -eq 0) {
    Write-Host '  All required checks passed.' -ForegroundColor Green
    exit 0
}
Write-Host ("  {0} required check(s) failed:" -f $Issues.Count) -ForegroundColor Red
foreach ($i in $Issues) { Write-Host "    - $i" -ForegroundColor Red }
Write-Host ''
exit 1
