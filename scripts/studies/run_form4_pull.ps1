<#
  run_form4_pull.ps1 — Full SEC EDGAR Form-4 pull for PIVOT Milestone F4-1, then the gate.

  Pulls every non-derivative Form-4 transaction for the 86 US single-stocks of tier_a into the
  resumable per-ticker cache (data/behavioral/form4/), then runs the pre-registered predictive gate
  (docs/backtest_out/form4_gate_prereg.md) and prints the PROCEED/CLOSED verdict.

  HEAVY: ~171k filings · ~5-7h · ~170k EDGAR requests (polite <=9 req/s). RESUMABLE — if it dies, just
  re-run; cached tickers are skipped. RUN FROM A TERMINAL THAT STAYS OPEN (agent-spawned heavy runs
  orphan over idle gaps). Everything is tee'd to docs/backtest_out/form4_pull_<stamp>.log.

  Coverage ceiling (honest): US single-stocks only ~= 41% of tier_a (ETFs have no insiders; .TO is SEDAR).
  Large-cap open-market buys are sparse, so a CLOSE is a live prior — that is still a decision-relevant result.

  Usage (from repo root):
    powershell -ExecutionPolicy Bypass -File scripts/studies/run_form4_pull.ps1
    powershell -ExecutionPolicy Bypass -File scripts/studies/run_form4_pull.ps1 -Workers 6 -Rps 9
    powershell -ExecutionPolicy Bypass -File scripts/studies/run_form4_pull.ps1 -Tickers AAPL -SkipGate   # smoke test
#>
param(
  [int]$Workers = 6,
  [double]$Rps = 9.0,
  [string]$Tickers = '',      # comma list override (else all 86 CIK-covered US tier_a)
  [int]$MaxTickers = 0,       # >0 caps ticker count (sampling)
  [switch]$SkipGate           # pull only; don't run the gate afterwards
)

# Native python writes its own errors to stdout; keep going on stderr noise (PS 5.1 wraps it).
$ErrorActionPreference = 'Continue'

# Repo root = two levels above scripts/studies/. Relative paths resolve from there.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

# UTF-8 everywhere so EDGAR/console output doesn't crash on cp1252 and logs stay clean.
$env:PYTHONIOENCODING = 'utf-8'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Error "venv python not found at $py"; exit 1 }

$stamp    = Get-Date -Format 'yyyyMMdd_HHmmss'
$logDir   = Join-Path $root 'docs\backtest_out'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pullLog  = Join-Path $logDir "form4_pull_$stamp.log"
$gateLog  = Join-Path $logDir "form4_gate_$stamp.log"
$cacheDir = Join-Path $root 'data\behavioral\form4'

function Log($msg) {
  $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $msg
  Write-Host $line -ForegroundColor Cyan
  Add-Content -Path $pullLog -Value $line -Encoding utf8
}

function Count-Cached {
  if (-not (Test-Path $cacheDir)) { return 0 }
  @(Get-ChildItem $cacheDir -Filter '*.parquet' -ErrorAction SilentlyContinue |
    Where-Object { $_.BaseName -notlike '_*' }).Count
}

$cachedBefore = Count-Cached

Log '==================================================================='
Log ' Form-4 FULL PULL  ->  PIVOT Milestone F4-1 (insider predictive gate)'
Log " repo:     $root"
Log " python:   $py"
Log " cache:    $cacheDir"
Log " cached:   $cachedBefore tickers already present -> will be SKIPPED (resume)"
Log " workers:  $Workers    rps: $Rps"
if ($Tickers)        { Log " tickers:  $Tickers (override)" }
if ($MaxTickers -gt 0) { Log " maxTickers: $MaxTickers (sampling)" }
Log ' estimate: ~171k filings / ~5-7h / ~170k EDGAR requests (polite)'
Log ' RESUMABLE: if this dies, re-run this script -- cached tickers are skipped.'
Log " pull log: $pullLog"
Log '==================================================================='

# ---- the pull (foreground in THIS terminal; per-ticker progress streams + is tee'd) ----
$pullArgs = @('scripts/studies/form4_fetch.py', '--workers', $Workers, '--rps', $Rps)
if ($Tickers)          { $pullArgs += @('--tickers', $Tickers) }
if ($MaxTickers -gt 0) { $pullArgs += @('--max-tickers', $MaxTickers) }

$sw = [System.Diagnostics.Stopwatch]::StartNew()
& $py @pullArgs 2>&1 | ForEach-Object {
  Write-Host $_
  Add-Content -Path $pullLog -Value ([string]$_) -Encoding utf8
}
$pullExit = $LASTEXITCODE
$sw.Stop()

$cachedAfter = Count-Cached
Log ('pull finished in {0:n1} min (exit {1}); cached tickers {2} -> {3}' -f `
     $sw.Elapsed.TotalMinutes, $pullExit, $cachedBefore, $cachedAfter)

if ($pullExit -ne 0) {
  Log 'PULL EXIT != 0 -- re-run this script to RESUME (cached tickers skip instantly). Gate NOT run.'
  exit $pullExit
}

if ($SkipGate) {
  Log 'SkipGate set -- pull complete. Run the gate manually:'
  Log "  $py scripts/studies/form4_gate.py --snapshot data/snapshot_2026-06-10"
  exit 0
}

# ---- the pre-registered gate (cheap; emits the PROCEED/CLOSED verdict + bars) ----
Log '-------------------------------------------------------------------'
Log " Running the pre-registered F4-1 gate  ->  $gateLog"
Log '-------------------------------------------------------------------'
& $py 'scripts/studies/form4_gate.py' '--snapshot' 'data/snapshot_2026-06-10' 2>&1 | ForEach-Object {
  Write-Host $_
  Add-Content -Path $gateLog -Value ([string]$_) -Encoding utf8
}
$gateExit = $LASTEXITCODE

Log "gate finished (exit $gateExit; 0 = PROCEED, 3 = CLOSED)."
Log "verdict + tables: $gateLog"
Log 'DONE.'
exit $gateExit
