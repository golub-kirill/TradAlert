#!/usr/bin/env python3
"""
Compare the max-hold (``time_stop``) exit across horizons and modes.

Loads the universe ONCE, then replays the portfolio baseline for each
(max_hold_days, mode) config and prints a single comparison table plus the
diagnostic ``time_stop`` cohort per run. Use it to pick / confirm the canonical
swing horizon for the validation program (see docs/adr/ADR-001-max-hold-exit.md).

Examples
--------
    python scripts/compare_max_hold.py                  # baseline + {15,30} x {hard, if_not_profit}
    python scripts/compare_max_hold.py --days 10 15 20  # custom caps
    python scripts/compare_max_hold.py --modes hard     # one mode only
    python scripts/compare_max_hold.py --start 2010-01-01 --end 2026-06-03

Each replay is a full portfolio walk (~3 min on the default universe), so the
default 5-config run takes ~15 min. Read-only: nothing is journaled or saved.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_cfg():
    import yaml
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = (
        [t for t in wl["tier_a"] if isinstance(t, str)]
        if "tier_a" in wl else wl.get("tickers", [])
    )
    return base_cfg, tickers


def _analyze(trades) -> dict:
    """All metrics derived directly from the trade ledger (no stats coupling)."""
    closed = [t for t in trades if getattr(t, "exit_date", None) is not None]
    rs = [t.effective_r for t in closed]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    total = sum(rs)
    out = {
        "n": n,
        "wr": (len(wins) / n * 100) if n else 0.0,
        "er": (total / n) if n else 0.0,
        "total": total,
        "pf": (sum(wins) / abs(sum(losses))) if losses else float("inf"),
        "avg_held": (sum(t.bars_held for t in closed) / n) if n else 0.0,
    }
    ts = [t for t in closed if t.exit_reason == "time_stop"]
    ts_rs = [t.effective_r for t in ts]
    out["ts_n"] = len(ts)
    out["ts_wr"] = (sum(1 for r in ts_rs if r > 0) / len(ts) * 100) if ts else 0.0
    out["ts_er"] = (sum(ts_rs) / len(ts)) if ts else 0.0
    return out


def _sharpe(trades):
    """Best-effort monthly Sharpe via the existing equity-curve builder."""
    try:
        from backtest.equity_curve import build_curve
        ec = build_curve([t for t in trades if t.exit_date is not None])
        for attr in ("sharpe", "sharpe_monthly", "sharpe_ratio"):
            v = getattr(ec, attr, None)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return None


def main() -> None:
    # UTF-8 stdout/stderr so ✓/box-drawing output survives piping on Windows.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Compare max-hold horizons/modes")
    ap.add_argument("--days", type=int, nargs="+", default=[15, 30],
                    metavar="N", help="Cap values to test (default: 15 30).")
    ap.add_argument("--modes", nargs="+", default=["hard", "if_not_profit"],
                    choices=["hard", "if_not_profit"],
                    help="Modes to test (default: both).")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD")
    args = ap.parse_args()

    from datetime import datetime

    def _d(s):
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None

    start, end = _d(args.start), _d(args.end)

    from backtest.loader import load_universe
    from backtest.sweep import SweepEngine

    base_cfg, tickers = _load_cfg()
    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_concurrent": 6,
        "earnings_aware": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.001),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
    }

    print(f"\n  Loading universe ({len(tickers)} tickers)…", flush=True)
    t0 = time.time()
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=_ROOT / "data" / "prices",
        earnings_dir=_ROOT / "data" / "earnings_history",
        start_date=start, end_date=end,
    )
    print(f"  {uni.summary()}  ({time.time() - t0:.1f}s)\n")

    configs = [("baseline (no cap)", None, None)]
    for d in args.days:
        for m in args.modes:
            configs.append((f"{d}d {m}", d, m))

    results = []
    for label, days, mode in configs:
        bp = dict(base_port)
        if days is not None:
            bp["max_hold_days"] = days
            bp["max_hold_mode"] = mode
        eng = SweepEngine(universe=uni, base_cfg=base_cfg,
                          base_port_cfg=bp, n_workers=1)
        t = time.time()
        pt = eng.baseline()
        a = _analyze(pt.trades)
        a["sharpe"] = _sharpe(pt.trades)
        a["label"] = label
        results.append(a)
        print(f"  ✓ {label:22s} {a['n']:5d}t  WR {a['wr']:4.1f}%  "
              f"E[R] {a['er']:+.3f}  Total {a['total']:+7.1f}R  "
              f"({time.time() - t:.0f}s)", flush=True)

    base_total = results[0]["total"]
    print("\n" + "=" * 94)
    print(f"  {'Config':22s} {'Trades':>6} {'WR%':>6} {'E[R]':>7} "
          f"{'TotalR':>8} {'dR':>7} {'PF':>5} {'Sharpe':>7} {'AvgHeld':>8}")
    print("  " + "-" * 92)
    for a in results:
        dr = a["total"] - base_total
        sh = f"{a['sharpe']:.2f}" if a["sharpe"] is not None else "—"
        pf = "inf" if a["pf"] == float("inf") else f"{a['pf']:.2f}"
        print(f"  {a['label']:22s} {a['n']:6d} {a['wr']:6.1f} {a['er']:+7.3f} "
              f"{a['total']:+8.1f} {dr:+7.1f} {pf:>5} {sh:>7} {a['avg_held']:8.1f}")
    print("  " + "-" * 92)
    print("  time_stop cohort  (what the cap actually cut):")
    for a in results:
        if a["ts_n"]:
            tag = ("cutting winners" if a["ts_wr"] > 55
                   else "cutting losers" if a["ts_wr"] < 45 else "mixed")
            print(f"    {a['label']:22s} {a['ts_n']:4d} cut   "
                  f"WR {a['ts_wr']:4.1f}%   E[R] {a['ts_er']:+.3f}   ({tag})")
    print("=" * 94)
    print("\n  Note: a cap frees portfolio slots, so trade populations differ "
          "between rows —\n  this is a portfolio-level comparison, not a strict "
          "per-trade A/B.\n")


if __name__ == "__main__":
    main()
