<#
Ensure the local Ollama server is running - the AI advisor's dependency.

Monitoring showed the scheduled scan hit "advisor unreachable" on ~40% of runs
(Ollama not up at scan time), so the advisor silently produced no verdict. This
helper is idempotent and FAIL-SAFE: it never throws and always exits 0, so a
down or missing Ollama can never block the scan - the advisor is fail-open and
simply omits its note.

Called from scripts/run_daily.bat before main.py. Can also be run standalone.
ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so
non-ASCII characters would corrupt parsing.
#>

$ErrorActionPreference = 'SilentlyContinue'
$Tags = 'http://127.0.0.1:11434/api/tags'

function Test-Ollama {
    try {
        $null = Invoke-WebRequest -Uri $Tags -TimeoutSec 3 -UseBasicParsing
        return $true
    } catch {
        return $false
    }
}

if (Test-Ollama) {
    Write-Output 'ensure_ollama: already running'
    exit 0
}

# Resolve the executable - Task Scheduler's PATH can be minimal, so fall back to
# the default Windows install location before giving up.
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) {
    $cand = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $cand) { $ollama = $cand }
}
if (-not $ollama) {
    Write-Output 'ensure_ollama: ollama executable not found (PATH / LOCALAPPDATA) - advisor will no-op'
    exit 0
}

Write-Output "ensure_ollama: not reachable - starting ollama serve ($ollama)"
try {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
} catch {
    Write-Output ('ensure_ollama: launch failed (' + $_.Exception.Message + ') - advisor will no-op')
    exit 0
}

# Give the server up to ~20s to accept connections, then proceed regardless.
for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Ollama) {
        Write-Output ('ensure_ollama: up after ' + $i + 's')
        exit 0
    }
}
Write-Output 'ensure_ollama: did not come up within 20s - advisor will no-op (fail-open)'
exit 0
