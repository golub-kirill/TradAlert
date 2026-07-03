#!/usr/bin/env python3
"""
Open-risk budget sweep — find the risk-adjusted operating point for max_open_risk.

Runs the headline config from filters.yaml (exit mode / cap / frictions as
configured) over the full watchlist, varying `portfolio.max_open_risk`, and prints
total effective-R, Sharpe, win rate, PF, and trade count per budget. Reuses the
production sweep machinery, so numbers match `run_backtest.py --sweep`. Exploratory
— does NOT journal.

    python scripts/studies/budget_sweep.py
    python scripts/studies/budget_sweep.py --budgets 3 4 5 6 7 8 --workers 8

Read-only on the DB. Needs the price cache (data/prices).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import yaml
    from backtest.loader import load_universe
    from backtest.sweep import SweepEngine, ParamSpec
    from backtest.equity_curve import build_curve

    ap = argparse.ArgumentParser(description="Open-risk budget (max_open_risk) sweep")
    ap.add_argument("--budgets", type=float, nargs="*",
                    default=[3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                    help="max_open_risk values to sweep")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]

    exec_cfg = base_cfg.get("execution", {})
    mh_days = exec_cfg.get("max_hold_days")
    mh_mode = str(exec_cfg.get("max_hold_mode", "hard")).replace("-", "_")

    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
                        earnings_aware=True)

    base_port = {
        "earnings_aware": True,
        "entry_slippage_pct": float(exec_cfg.get("entry_slippage_pct", 0.002)),
        "commission_r": float(exec_cfg.get("commission_r", 0.005)),
        "close_open_at_eod": True,
    }
    if mh_days is not None:
        base_port["max_hold_days"] = int(mh_days)
        base_port["max_hold_mode"] = mh_mode

    engine = SweepEngine(uni, base_cfg=base_cfg, base_port_cfg={**base_port, "max_open_risk": 5.0},
                         n_workers=args.workers)

    spec = ParamSpec("portfolio.max_open_risk", tuple(args.budgets),
                     "Open-risk budget", "portfolio", fmt="{:.1f}")
    print("  Running budget sweep…", flush=True)
    report = engine.run_ofat(grid=[], port_grid=[spec])

    def _row(label, pt):
        ec = build_curve(pt.trades)
        s = pt.stats
        return (f"  {label:>10}  R {s.total_r:+8.1f}  Sharpe {ec.sharpe:5.2f}  "
                f"WR {s.win_rate * 100:4.0f}%  PF {min(s.profit_factor, 999):5.2f}  "
                f"trades {s.trades_count}")

    print("\n" + "=" * 72)
    cap = f"{int(mh_days)}d {mh_mode}" if mh_days is not None else "no cap"
    print(f"  Open-risk budget sweep  ·  {cap}  ·  slippage "
          f"{base_port['entry_slippage_pct']:.4f}  commission {base_port['commission_r']:.4f}")
    print("  " + "-" * 68)
    print(_row("base 5.0", report.baseline))
    print()
    pts = [p for p in report.points if p.param_label == spec.label]
    for p in sorted(pts, key=lambda x: float(x.param_value)):
        print(_row(spec.fmt.format(float(p.param_value)), p))
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
