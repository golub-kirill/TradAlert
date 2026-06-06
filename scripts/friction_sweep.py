#!/usr/bin/env python3
"""
Friction sensitivity sweep — how hard do slippage / commission bite the edge?

Runs the headline config (25-bar hard cap, open-risk budget 5.0) over the full
watchlist, varying one friction knob at a time, and prints total effective-R,
Sharpe, win rate, PF, and trade count per value. Reuses the production sweep
machinery (`SweepEngine.run_ofat` over a one-knob portfolio grid), so the numbers
match `run_backtest.py --sweep`. Exploratory — does NOT journal.

    python scripts/friction_sweep.py
    python scripts/friction_sweep.py --slippage 0 0.001 0.002 0.003
    python scripts/friction_sweep.py --commission 0 0.003 0.005 0.01 --workers 8

Read-only on the DB. Needs the price cache (data/prices).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
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

    ap = argparse.ArgumentParser(description="Friction (slippage/commission) sensitivity sweep")
    ap.add_argument("--slippage", type=float, nargs="*",
                    default=[0.0, 0.0005, 0.001, 0.002, 0.003],
                    help="entry_slippage_pct values to sweep")
    ap.add_argument("--commission", type=float, nargs="*",
                    default=[0.0, 0.003, 0.005, 0.01],
                    help="commission_r values to sweep")
    ap.add_argument("--max-hold-days", type=int, default=25)
    ap.add_argument("--max-open-risk", type=float, default=5.0)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]

    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
                        earnings_aware=True)

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": args.max_open_risk,
        "earnings_aware": True,
        "entry_slippage_pct": float(exec_cfg.get("entry_slippage_pct", 0.002)),
        "commission_r": float(exec_cfg.get("commission_r", 0.005)),
        "close_open_at_eod": True,
        "max_hold_days": args.max_hold_days,
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "hard")).replace("-", "_"),
    }
    engine = SweepEngine(uni, base_cfg=base_cfg, base_port_cfg=base_port,
                         n_workers=args.workers)

    slip_spec = ParamSpec("portfolio.entry_slippage_pct", tuple(args.slippage),
                          "Entry slippage %", "portfolio", fmt="{:.4f}")
    comm_spec = ParamSpec("portfolio.commission_r", tuple(args.commission),
                          "Commission (R)", "portfolio", fmt="{:.4f}")
    print("  Running friction sweep…", flush=True)
    report = engine.run_ofat(grid=[], port_grid=[slip_spec, comm_spec])

    def _row(label, pt):
        ec = build_curve(pt.trades)
        s = pt.stats
        return (f"  {label:>10}  R {s.total_r:+8.1f}  Sharpe {ec.sharpe:5.2f}  "
                f"WR {s.win_rate * 100:4.0f}%  PF {min(s.profit_factor, 999):5.2f}  "
                f"trades {s.trades_count}")

    base = report.baseline
    print("\n" + "=" * 72)
    print(f"  Headline baseline  ·  slippage {base_port['entry_slippage_pct']:.4f}  "
          f"commission {base_port['commission_r']:.4f}  ·  {args.max_hold_days}d hard  "
          f"budget {args.max_open_risk:g}")
    print("  " + "-" * 68)
    print(_row("baseline", base))

    for spec in (slip_spec, comm_spec):
        pts = [p for p in report.points if p.param_label == spec.label]
        if not pts:
            continue
        print(f"\n  {spec.label} sensitivity:")
        for p in sorted(pts, key=lambda x: float(x.param_value)):
            print(_row(spec.fmt.format(float(p.param_value)), p))
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
