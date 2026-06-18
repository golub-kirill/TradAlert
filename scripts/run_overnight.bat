@echo off
REM Overnight measurement program — run in a window you keep open.
REM Four steps, ~3h studies + the V5 walk-forward. Every step streams to its
REM own timestamped log under docs\backtest_out\ AND shows live here.
REM Studies read the FROZEN snapshot (data\snapshot_2026-06-10) — immune to
REM cache refreshes; V5 reads live caches (it is a fresh measurement).
cd /d C:\PycharmProjects\TradAlert
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

set TEE="C:\Program Files\Git\usr\bin\tee.exe"
if not exist %TEE% (
    echo tee.exe not found at %TEE% — install Git for Windows or fix the path.
    pause
    exit /b 1
)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i

echo ============================================================
echo  Step 1/4 - B3 venue economics (4 legs, ~20 min)
echo  HARNESS GATE: the full@0.002 leg must show +120.42R / 1622
echo  trades (run_id=15). If it does not, STOP and report.
echo  Log: docs\backtest_out\study_b3_%TS%.log
echo ============================================================
.venv\Scripts\python.exe scripts\study_matrix.py --study b3 --dump-trades docs\backtest_out\studies\b3 2>&1 | %TEE% -a "docs\backtest_out\study_b3_%TS%.log"

echo.
echo ============================================================
echo  Step 2/4 - B1 drawdown defense (10 legs, ~55 min)
echo  Log: docs\backtest_out\study_b1_%TS%.log
echo ============================================================
.venv\Scripts\python.exe scripts\study_matrix.py --study b1 --dump-trades docs\backtest_out\studies\b1 2>&1 | %TEE% -a "docs\backtest_out\study_b1_%TS%.log"

echo.
echo ============================================================
echo  Step 3/4 - B2 turnover frontier (16 legs, ~90 min)
echo  Log: docs\backtest_out\study_b2_%TS%.log
echo ============================================================
.venv\Scripts\python.exe scripts\study_matrix.py --study b2 --dump-trades docs\backtest_out\studies\b2 2>&1 | %TEE% -a "docs\backtest_out\study_b2_%TS%.log"

echo.
echo ============================================================
echo  Step 4/4 - V5 joint walk-forward (14 workers, several hours)
echo  Log: docs\backtest_out\v5_joint_wf_%TS%.log
echo ============================================================
.venv\Scripts\python.exe -m backtest.run_backtest --start 2000-01-01 --walk-forward --wf-joint 24 --workers 14 --no-journal --no-html --no-csv 2>&1 | %TEE% -a "docs\backtest_out\v5_joint_wf_%TS%.log"

echo.
echo ============================================================
echo  ALL DONE - logs saved under docs\backtest_out\:
echo    study_b3_%TS%.log   study_b1_%TS%.log
echo    study_b2_%TS%.log   v5_joint_wf_%TS%.log
echo  Trades dumps: docs\backtest_out\studies\
echo ============================================================
pause
