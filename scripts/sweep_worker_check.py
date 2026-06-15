"""Worker-path gate for the SweepEngine._run_one refactor (G2-C deep check).

Runs a tiny OFAT sweep on the pinned snapshot TWICE — n_workers=1 (sequential,
in-process _run_one) and n_workers=2 (the ProcessPoolExecutor path where
_SweepRunHelper aliases _run_one and the module-level helpers must resolve inside
worker processes). The grid touches a PORTFOLIO param (breakeven → _build_port_config)
and a SETTINGS param (behavioral → _job_settings) so both helpers run in a worker.

PASS = the baseline reproduces run_id=15 AND every grid point is identical between
the sequential and parallel runs (deterministic; the worker path == in-process).

Usage: python scripts/sweep_worker_check.py [--snapshot data/snapshot_2026-06-10]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.sweep import ParamSpec, SweepEngine  # noqa: E402


def _key(pt) -> tuple:
    """Identity of a sweep point for comparison (deterministic stats)."""
    return (pt.run_id, pt.stats.trades_count,
            round(pt.stats.expectancy_r, 6), round(build_curve(pt.trades).total_r, 4))


def _run(uni, base_cfg, base_port, grid, port_grid, n_workers):
    eng = SweepEngine(uni, base_cfg, base_port, n_workers=n_workers)
    rep = eng.run_ofat(grid=grid, port_grid=port_grid)
    pts = {pt.run_id: _key(pt) for pt in [rep.baseline, *rep.points]}
    return rep, pts


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    base_cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]
    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 5.0, "earnings_aware": True, "close_open_at_eod": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
        "breakeven_trigger_r": float(exec_cfg.get("breakeven_trigger_r", 1.0) or 1.0),
    }
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200), earnings_aware=True,
        cache_dir=snap / "prices", earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro", behavioral_dir=snap / "behavioral", start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)

    # one settings-resident leg (_job_settings) + one portfolio leg (_build_port_config)
    grid = [ParamSpec("behavioral.size_mult_floor", (0.5,), "Behavioral floor", "phase8")]
    port_grid = [ParamSpec("portfolio.breakeven_trigger_r", (1.5,), "Breakeven (R)", "exits")]

    print("  running n_workers=1 (sequential) ...", flush=True)
    rep1, pts1 = _run(uni, base_cfg, base_port, grid, port_grid, 1)
    print("  running n_workers=2 (parallel) ...", flush=True)
    rep2, pts2 = _run(uni, base_cfg, base_port, grid, port_grid, 2)

    print()
    bl = _key(rep1.baseline)
    print(f"  baseline: trades={bl[1]} totalR={bl[3]:+.2f}  "
          f"(GATE: 1622 / +120.42)")
    ok = (pts1 == pts2)
    print(f"  sequential vs parallel points IDENTICAL: {ok}")
    if not ok:
        for rid in sorted(set(pts1) | set(pts2)):
            a, b = pts1.get(rid), pts2.get(rid)
            flag = "" if a == b else "   <<< MISMATCH"
            print(f"    {rid}\n      seq={a}\n      par={b}{flag}")
    else:
        for rid, k in pts1.items():
            print(f"    {rid}: trades={k[1]} E[R]={k[2]:+.3f} totalR={k[3]:+.2f}")


if __name__ == "__main__":
    main()
