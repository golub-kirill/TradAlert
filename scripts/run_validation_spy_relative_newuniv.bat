@echo off
REM ===========================================================================
REM  SPY-RELATIVE re-test on the NEW 227-name universe (2026-06-24 re-balance).
REM  Same frozen criteria as docs\backtest_out\phase23_spy_relative_prereg.md
REM  (re-run pre-registration appended 2026-06-25). Logs go to *_newuniv.log so
REM  the old-universe (213-name) logs are PRESERVED for comparison.
REM
REM  Run in a window you KEEP OPEN (agent-spawned heavy runs orphan). Output is
REM  VERBOSE: a baseline-result line, then per-config "[n/256] J045 -> 1650t
REM  E[R]+0.07 WR43% [elapsed - ETA]" streamed live to console AND the log.
REM
REM  Sets PYTHONIOENCODING=utf-8 so the non-ASCII progress glyphs don't crash a
REM  cp1252 console (the raw `> log` form WOULD crash). All legs read the FROZEN
REM  snapshot. Runtime: ~1.5h per joint seed (256 configs / 14 workers) + the
REM  WF-vs-SPY ~30-45 min; budget ~3-4 h total.
REM ===========================================================================
cd /d C:\PycharmProjects\TradAlert
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set SNAP=data\snapshot_2026-06-10

set TEE="C:\Program Files\Git\usr\bin\tee.exe"
if not exist %TEE% (
    echo tee.exe not found at %TEE% - install Git for Windows or fix the path.
    pause
    exit /b 1
)

echo ============================================================
echo  Phase 2a/3 - JOINT multiple-testing vs SPY  (seed 1337) - NEW 227 universe
echo  Log: docs\backtest_out\phase2_mt_joint_vs_spy_1337_newuniv.log
echo ============================================================
.venv\Scripts\python.exe scripts\multiple_testing.py --snapshot %SNAP% --joint 256 --joint-knobs 3 --joint-seed 1337 --spy-relative --workers 14 --bootstrap 10000 --seed 7 --breakeven-trigger-r 1.0 2>&1 | %TEE% "docs\backtest_out\phase2_mt_joint_vs_spy_1337_newuniv.log"

echo.
echo ============================================================
echo  Phase 2b/3 - JOINT multiple-testing vs SPY  (seed 4242) - NEW 227 universe
echo  Both seeds must agree for a non-FAIL verdict (no seed-shopping).
echo  Log: docs\backtest_out\phase2_mt_joint_vs_spy_4242_newuniv.log
echo ============================================================
.venv\Scripts\python.exe scripts\multiple_testing.py --snapshot %SNAP% --joint 256 --joint-knobs 3 --joint-seed 4242 --spy-relative --workers 14 --bootstrap 10000 --seed 7 --breakeven-trigger-r 1.0 2>&1 | %TEE% "docs\backtest_out\phase2_mt_joint_vs_spy_4242_newuniv.log"

echo.
echo ============================================================
echo  Phase 3 - fixed-config WALK-FORWARD vs SPY - NEW 227 universe
echo  (already per-window verbose via the WF engine progress callback)
echo  Log: docs\backtest_out\phase3_wf_vs_spy_newuniv.log
echo ============================================================
.venv\Scripts\python.exe scripts\wf_benchmark_relative.py --snapshot %SNAP% 2>&1 | %TEE% "docs\backtest_out\phase3_wf_vs_spy_newuniv.log"

echo.
echo ============================================================
echo  ALL DONE - new-universe SPY-relative logs under docs\backtest_out\:
echo    phase2_mt_joint_vs_spy_1337_newuniv.log
echo    phase2_mt_joint_vs_spy_4242_newuniv.log
echo    phase3_wf_vs_spy_newuniv.log
echo  Record the verdict in phase23_spy_relative_prereg.md (criteria UNCHANGED).
echo ============================================================
pause
