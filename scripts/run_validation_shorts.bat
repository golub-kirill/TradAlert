@echo off
REM ===========================================================================
REM  SHORT-SIDE validation — paired (long-only vs allow_shorts), borrow-honest,
REM  vs SPY. Run in a window you KEEP OPEN. Single ~10-min job (two paired legs
REM  on one snapshot load). Don't run concurrently with the multi-hour SPY-relative
REM  joint sweep — they contend for the 14 cores.
REM
REM  Pre-registered (FROZEN): docs\backtest_out\shorts_validation_prereg.md
REM  Bars: gate-in 30+ BEAR shorts; Bar1 Sharpe/Calmar on at-least off (effective_r);
REM  Bar2 excess-Sharpe vs SPY ON at-least OFF (base + band); Bar3 bear-window insurance
REM  (2-of-3 of 2008/2020/2022); Bar4 full maxDD ON at-most OFF+2R. SHIP only if ALL pass.
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

echo ============================================================
echo  Short-side validation (paired snapshot, vs SPY)
echo  Log: docs\backtest_out\shorts_validate_vs_spy.log
echo ============================================================
.venv\Scripts\python.exe scripts\shorts_validate.py --snapshot data\snapshot_2026-06-10 --save-ledgers 2>&1 | %TEE% "docs\backtest_out\shorts_validate_vs_spy.log"

echo.
echo ============================================================
echo  DONE - record the verdict in shorts_validation_prereg.md
echo  (criteria UNCHANGED). allow_shorts stays false unless SHIP.
echo ============================================================
pause
