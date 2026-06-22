@echo off
REM ===========================================================================
REM  SPY-RELATIVE robustness — the corrected, multiple-testing-honest, benchmark-
REM  relative verdict (the program's Phase 2 "joint vs SPY" + Phase 3 "WF vs SPY",
REM  now BUILT). Run in a window you KEEP OPEN (agent-spawned heavy runs orphan).
REM
REM  Pre-registered: docs\backtest_out\phase23_spy_relative_prereg.md  (bars frozen
REM  BEFORE this runs — do not edit a criterion after seeing a result).
REM
REM  All legs read the FROZEN snapshot (immune to cache jitter; SPY lives there too).
REM  Config = COT-only headline (positioning is COT-only via SweepEngine's cached
REM  settings.yaml so live equals backtest).
REM
REM  Runtime: each joint seed ~1.5h (256 configs x full backtest / 14 workers);
REM  the WF-vs-SPY ~30-45 min (sequential). Budget ~3-4 h total.
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
echo  Phase 2a/3 - JOINT multiple-testing vs SPY  (seed 1337)
echo  256 random 3-knob configs; DSR + White's RC vs cash AND
echo  vs SPY (0.5/1/2%% alpha-band). PASS = RC p below 0.05 vs SPY AND
echo  joint DSR above 0.95. Prior: DSR sub-0.95, loses to SPY (Phase 1).
echo  Log: docs\backtest_out\phase2_mt_joint_vs_spy_1337.log
echo ============================================================
.venv\Scripts\python.exe scripts\multiple_testing.py --snapshot %SNAP% --joint 256 --joint-knobs 3 --joint-seed 1337 --spy-relative --workers 14 --bootstrap 10000 --seed 7 --breakeven-trigger-r 1.0 2>&1 | %TEE% "docs\backtest_out\phase2_mt_joint_vs_spy_1337.log"

echo.
echo ============================================================
echo  Phase 2b/3 - JOINT multiple-testing vs SPY  (seed 4242)
echo  Second seed - report BOTH, no seed-shopping. Both seeds
echo  must agree for a non-FAIL verdict.
echo  Log: docs\backtest_out\phase2_mt_joint_vs_spy_4242.log
echo ============================================================
.venv\Scripts\python.exe scripts\multiple_testing.py --snapshot %SNAP% --joint 256 --joint-knobs 3 --joint-seed 4242 --spy-relative --workers 14 --bootstrap 10000 --seed 7 --breakeven-trigger-r 1.0 2>&1 | %TEE% "docs\backtest_out\phase2_mt_joint_vs_spy_4242.log"

echo.
echo ============================================================
echo  Phase 3 - fixed-config WALK-FORWARD vs SPY
echo  Per-OOS-window excess-Sharpe + beat?, pooled OOS exSh.
echo  PASS = at least 60%% OOS windows beat SPY AND pooled exSh positive.
echo  Byte-identical config to heavy_wf_fixed (COT-only).
echo  Log: docs\backtest_out\phase3_wf_vs_spy.log
echo ============================================================
.venv\Scripts\python.exe scripts\wf_benchmark_relative.py --snapshot %SNAP% 2>&1 | %TEE% "docs\backtest_out\phase3_wf_vs_spy.log"

echo.
echo ============================================================
echo  ALL DONE - SPY-relative logs under docs\backtest_out\:
echo    phase2_mt_joint_vs_spy_1337.log  phase2_mt_joint_vs_spy_4242.log
echo    phase3_wf_vs_spy.log
echo  Record the verdict in phase23_spy_relative_prereg.md (criteria UNCHANGED).
echo ============================================================
pause
