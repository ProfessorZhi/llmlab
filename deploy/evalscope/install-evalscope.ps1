#Requires -Version 5.1
<#
    LLM Lab - install EvalScope into the project's own virtual environment.

    Everything lands in evaluation\.venv.  The global Python interpreter is
    never modified: no --user installs, no site-packages writes, no elevation.

    The package index defaults to the Tsinghua mirror because that is reachable
    from this workstation; override with -IndexUrl for any other PyPI.

    ASCII only - see the note in start.ps1 about the GBK/ANSI decoding trap.

    Usage:
      powershell -ExecutionPolicy Bypass -File deploy\evalscope\install-evalscope.ps1
      powershell -ExecutionPolicy Bypass -File deploy\evalscope\install-evalscope.ps1 -IndexUrl https://pypi.org/simple
#>
[CmdletBinding()]
param(
    [string] $IndexUrl = 'https://pypi.tuna.tsinghua.edu.cn/simple',
    [string] $PythonExe = '',
    [string] $Extra = 'service'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$EvalDir = Join-Path $Root 'evaluation'
$VenvDir = Join-Path $EvalDir '.venv'
$VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'

function Write-Line {
    param([string]$Message, [string]$Level = 'INFO')
    $color = 'Gray'
    if ($Level -eq 'WARN')  { $color = 'Yellow' }
    if ($Level -eq 'ERROR') { $color = 'Red' }
    if ($Level -eq 'OK')    { $color = 'Green' }
    Write-Host ("[{0}] {1}" -f $Level, $Message) -ForegroundColor $color
}

Write-Host ''
Write-Host '  LLM Lab - EvalScope installer' -ForegroundColor Cyan
Write-Host ''

# ------------------------------------------------------------------ base ----
if ($PythonExe -eq '') {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $PythonExe = $cmd.Source }
}
if ($PythonExe -eq '') {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $PythonExe = $cmd.Source }
}
if ($PythonExe -eq '' -or -not (Test-Path $PythonExe)) {
    Write-Line 'no usable Python interpreter found. Install Python 3.11 or newer.' 'ERROR'
    exit 2
}

$ver = (& $PythonExe -c "import sys;print('%d.%d.%d' % sys.version_info[:3])").Trim()
Write-Line "base interpreter: $PythonExe (Python $ver)"
$maj, $min = [int]$ver.Split('.')[0], [int]$ver.Split('.')[1]
if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 10)) {
    Write-Line 'EvalScope requires Python 3.10 or newer.' 'ERROR'
    exit 2
}
# A stale EvalScope floor: tuna often lags the real release.  Recorded here so
# the next person does not mistake a mirror lag for "this is the latest".
Write-Line 'note: mirror indexes can lag pypi.org; cross-check the version if it looks old.'

# ------------------------------------------------------------------ venv ----
New-Item -ItemType Directory -Force -Path $EvalDir | Out-Null
if (-not (Test-Path $VenvPy)) {
    Write-Line "creating virtual environment at $VenvDir"
    & $PythonExe -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) {
        Write-Line 'venv creation failed.' 'ERROR'
        exit 3
    }
    Write-Line 'venv created' 'OK'
} else {
    Write-Line "reusing existing venv at $VenvDir"
}

# ---------------------------------------------------------------- install ---
Write-Line "upgrading pip from $IndexUrl"
& $VenvPy -m pip install --upgrade pip -i $IndexUrl
if ($LASTEXITCODE -ne 0) { Write-Line 'pip upgrade failed' 'WARN' }

$spec = "evalscope[$Extra]"
Write-Line "installing $spec (this pulls a large dependency set and can take a while)"
& $VenvPy -m pip install $spec -i $IndexUrl
if ($LASTEXITCODE -ne 0) {
    Write-Line "installation of $spec failed (exit $LASTEXITCODE)." 'ERROR'
    Write-Line 'Common causes: mirror missing a wheel, no network, or a Python version floor.' 'ERROR'
    exit 4
}

# -------------------------------------------------------------- validate ----
$check = & $VenvPy -c "import evalscope,sys;print(evalscope.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Line "evalscope imported but failed: $check" 'ERROR'
    exit 5
}
Write-Line "evalscope $check installed" 'OK'

$exe = Join-Path $VenvDir 'Scripts\evalscope.exe'
if (Test-Path $exe) {
    Write-Line "cli: $exe" 'OK'
    Write-Line 'start it with: legacy/windows-tools/start.ps1  (the dashboard binds localhost:9000)'
} else {
    Write-Line 'evalscope console script was not created; the module is importable though.' 'WARN'
}

Write-Host ''
Write-Line 'done. The global Python was not modified.' 'OK'
Write-Host ''
exit 0
