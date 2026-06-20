@echo off
REM ===========================================================================
REM  Heavy validation runs — run in a window you KEEP OPEN (these orphan when
REM  agent-spawned, and the V5 walk-forward is multi-hour). Each step streams to
REM  its own timestamped log under docs\backtest_out\ AND shows live here.
REM
REM  These are the CONFIRMATORY validation phases (the program already PIVOTED to
REM  the beta sleeve, DESIGN D-007) — run them for the record / on demand.
REM
REM  Phase 0 reads the FROZEN snapshot (immune to cache refreshes). The
REM  multiple-testing + walk-forward steps read LIVE caches (fresh measurements;
REM  headline R carries +-~10R cache jitter, so judge them on Sharpe/ratios, not
REM  the absolute R level).
REM
REM  NOTE: the program's "joint vs SPY" (Phase 2) and "WF vs SPY" (Phase 3)
REM  ENHANCEMENTS are NOT built (multiple_testing.py is OFAT + vs-cash). For the
REM  per-window SPY-relative read, run scripts\benchmark_relative.py separately
REM  (fast, seconds) — it is the built Phase-1 metric.
REM ===========================================================================
cd /d C:\PycharmProjects\TradAlert
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

set TEE="C:\Program Files\Git\usr\bin\tee.exe"
if not exist %TEE% (
    echo tee.exe not found at %TEE% - install Git for Windows or fix the path.
    pause
    exit /b 1
)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i

echo ============================================================
echo  Step 1/4 - Phase 0 GATE: reproduce the headline (seconds)
echo  The breakeven-1.0R leg MUST show +113.58R / 1635 trades /
echo  Sharpe 0.57 (the pinned-snapshot 3-axis headline). If it
echo  does NOT, STOP - the snapshot or code has drifted.
echo  Log: docs\backtest_out\heavy_phase0_%TS%.log
echo ============================================================
.venv\Scripts\python.exe scripts\paired_ab.py --snapshot data\snapshot_2026-06-10 2>&1 | %TEE% -a "docs\backtest_out\heavy_phase0_%TS%.log"

echo.
echo ============================================================
echo  Step 2/4 - Deflated Sharpe + White's reality check (OFAT
echo  sweep grid, 10k bootstrap; reads LIVE caches; ~30-60 min)
echo  Multiple-testing correction on the parameter search. The
echo  prior verdict is DSR ~0.91 (sub-0.95, optimistic bound).
echo  Log: docs\backtest_out\heavy_deflated_sharpe_%TS%.log
echo ============================================================
.venv\Scripts\python.exe scripts\multiple_testing.py --workers 14 --bootstrap 10000 --seed 7 --breakeven-trigger-r 1.0 2>&1 | %TEE% -a "docs\backtest_out\heavy_deflated_sharpe_%TS%.log"

echo.
echo ============================================================
echo  Step 3/4 - Fixed-config walk-forward (OOS temporal
echo  stability; --wf-no-retune; 14 workers; ~20-40 min)
echo  Prior: ~0 IS->OOS degradation, ~70%% OOS-positive.
echo  Log: docs\backtest_out\heavy_wf_fixed_%TS%.log
echo ============================================================
.venv\Scripts\python.exe -m backtest.run_backtest --start 2000-01-01 --walk-forward --wf-no-retune --workers 14 --no-journal --no-html --no-csv 2>&1 | %TEE% -a "docs\backtest_out\heavy_wf_fixed_%TS%.log"

echo.
echo ============================================================
echo  Step 4/4 - V5 joint re-tune walk-forward (search-inflation
echo  estimate; 14 workers; SEVERAL HOURS)
echo  Prior: IS +0.171 -> OOS +0.072 = +0.099 selection inflation
echo  (tuning buys ~nothing OOS -> freeze the config).
echo  Log: docs\backtest_out\heavy_wf_joint_%TS%.log
echo ============================================================
.venv\Scripts\python.exe -m backtest.run_backtest --start 2000-01-01 --walk-forward --wf-joint 24 --workers 14 --no-journal --no-html --no-csv 2>&1 | %TEE% -a "docs\backtest_out\heavy_wf_joint_%TS%.log"

echo.
echo ============================================================
echo  ALL DONE - logs under docs\backtest_out\:
echo    heavy_phase0_%TS%.log          heavy_deflated_sharpe_%TS%.log
echo    heavy_wf_fixed_%TS%.log        heavy_wf_joint_%TS%.log
echo  For the per-window SPY-relative read run separately:
echo    .venv\Scripts\python.exe scripts\benchmark_relative.py
echo ============================================================
pause
