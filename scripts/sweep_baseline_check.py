"""Deterministic gate for the SweepEngine._run_one refactor (G2-C).

Builds the SweepEngine on the pinned snapshot with the run_id=15 headline config
and runs .baseline(), which goes through the refactored hot path: engine build →
_build_port_config → _job_settings (cached settings) → run_prepped → _collect_point.
It MUST reproduce run_id=15 (1622 trades / +120.42R / Sharpe 0.60 / maxDD 30.71).
If a digit moves, the _run_one decomposition or the settings hoist perturbed it.

Usage: python scripts/sweep_baseline_check.py [--snapshot data/snapshot_2026-06-10]
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
from backtest.sweep import SweepEngine  # noqa: E402


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
        "max_open_risk": 5.0,
        "earnings_aware": True,
        "close_open_at_eod": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
        "breakeven_trigger_r": float(exec_cfg.get("breakeven_trigger_r", 1.0) or 1.0),
    }

    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral",
        start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)

    eng = SweepEngine(uni, base_cfg, base_port, n_workers=1)
    pt = eng.baseline()
    ec = build_curve(pt.trades)
    st = pt.stats
    print()
    print("  SweepEngine.baseline() — GATE: must = run_id=15 "
          "(1622t / +120.42R / 0.60 / 30.71)")
    print(f"  trades={st.trades_count}  E[R]={st.expectancy_r:+.3f}  "
          f"totalR={ec.total_r:+.2f}  Sharpe={ec.sharpe:.2f}  "
          f"Sortino={ec.sortino:.2f}  maxDD={ec.max_dd:.2f}")


if __name__ == "__main__":
    main()
