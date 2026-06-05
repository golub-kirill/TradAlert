#!/usr/bin/env python3
"""
A/B the chronic-loser penalty: baseline (OFF) vs --chronic-penalty (ON).

The penalty is a VARIANCE tool, not an edge source, so judge it on risk-adjusted
metrics — Sharpe / Calmar / max-drawdown — NOT total R. Keep it only if it buys a
drawdown / Sharpe improvement worth the return it gives up. (Ledger analysis on
2026-06-03 showed per-ticker losing streaks carry only a weak forward signal and
that forward EV stays positive at 4+ losses; the schedule was de-fanged to floor
at 0.25 instead of blocking — see docs/triage_raw_notes_2026-06.md, Note 4.)

Loads the universe once, replays the baseline twice (penalty off, then on with
the filters.yaml `chronic_loser_penalty` schedule), and prints a side-by-side
table. Read-only — nothing is journaled or saved.

    python scripts/ab_chronic_penalty.py
    python scripts/ab_chronic_penalty.py --start 2010-01-01 --end 2026-06-03
"""

from __future__ import annotations

import argparse
import math
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


def _metrics(trades) -> dict:
    from backtest.equity_curve import build_curve
    closed = [t for t in trades if t.exit_date is not None]
    rs = [t.effective_r for t in closed]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    ec = build_curve(closed)
    return {
        "n": n,
        "wr": (100 * len(wins) / n) if n else 0.0,
        "er": (sum(rs) / n) if n else 0.0,
        "total": ec.total_r,
        "sharpe": ec.sharpe,
        "sortino": ec.sortino,
        "calmar": ec.calmar,
        "max_dd": ec.max_dd,
        "pos_mo": ec.pct_positive_months * 100,
    }


def _f(v, d=2):
    return "∞" if (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{d}f}"


def main() -> None:
    # UTF-8 stdout/stderr so ✓/→/Δ/∞ output survives piping on Windows.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="A/B the chronic-loser penalty")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD")
    args = ap.parse_args()

    from datetime import datetime

    def _d(s):
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None

    from backtest.loader import load_universe
    from backtest.sweep import SweepEngine

    base_cfg, tickers = _load_cfg()
    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 6.0,
        "earnings_aware": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.001),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
    }
    chronic_block = dict(base_cfg.get("chronic_loser_penalty", {}) or {})
    print(f"\n  Chronic schedule under test: lookback="
          f"{chronic_block.get('lookback_days', 90)}d  scale={chronic_block.get('scale')}")

    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    t0 = time.time()
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=_ROOT / "data" / "prices",
        earnings_dir=_ROOT / "data" / "earnings_history",
        start_date=_d(args.start), end_date=_d(args.end),
    )
    print(f"  {uni.summary()}  ({time.time() - t0:.1f}s)\n")

    runs = [
        ("baseline (penalty OFF)", None),
        ("chronic penalty ON", {**chronic_block, "enabled": True}),
    ]
    res = []
    for label, chronic_cfg in runs:
        bp = dict(base_port)
        if chronic_cfg is not None:
            bp["chronic_loser_cfg"] = chronic_cfg
        eng = SweepEngine(universe=uni, base_cfg=base_cfg,
                          base_port_cfg=bp, n_workers=1)
        t = time.time()
        pt = eng.baseline()
        m = _metrics(pt.trades)
        m["label"] = label
        res.append(m)
        print(f"  ✓ {label:24s} {m['n']:5d}t  TotalR {m['total']:+7.1f}  "
              f"Sharpe {_f(m['sharpe'])}  Calmar {_f(m['calmar'])}  "
              f"maxDD {m['max_dd']:.1f}R  ({time.time() - t:.0f}s)", flush=True)

    off, on = res[0], res[1]
    rows = [
        ("Trades",         f"{off['n']}",          f"{on['n']}",          ""),
        ("Win rate %",     f"{off['wr']:.1f}",     f"{on['wr']:.1f}",     f"{on['wr']-off['wr']:+.1f}"),
        ("Expectancy R",   f"{off['er']:+.3f}",    f"{on['er']:+.3f}",    f"{on['er']-off['er']:+.3f}"),
        ("Total R",        f"{off['total']:+.1f}", f"{on['total']:+.1f}", f"{on['total']-off['total']:+.1f}"),
        ("Sharpe",         _f(off['sharpe']),      _f(on['sharpe']),      _f(on['sharpe']-off['sharpe'])),
        ("Sortino",        _f(off['sortino']),     _f(on['sortino']),     _f(on['sortino']-off['sortino'])),
        ("Calmar",         _f(off['calmar']),      _f(on['calmar']),      ""),
        ("Max drawdown R", f"{off['max_dd']:.1f}", f"{on['max_dd']:.1f}", f"{on['max_dd']-off['max_dd']:+.1f}"),
        ("Positive mo %",  f"{off['pos_mo']:.0f}", f"{on['pos_mo']:.0f}", f"{on['pos_mo']-off['pos_mo']:+.0f}"),
    ]
    print("\n" + "=" * 72)
    print(f"  {'Metric':16s} {'OFF':>11} {'ON':>11} {'Δ (ON-OFF)':>12}")
    print("  " + "-" * 68)
    for name, a, b, d in rows:
        print(f"  {name:16s} {a:>11} {b:>11} {d:>12}")
    print("=" * 72)

    print("\n  Read: the penalty is a variance tool — keep it ONLY if it lifts")
    print("  risk-adjusted return (Sharpe/Calmar UP, max-DD DOWN) without giving")
    print("  up too much Total R. A pure Total-R drop with no DD/Sharpe gain = cost.")
    sharpe_up = (math.isfinite(on['sharpe']) and math.isfinite(off['sharpe'])
                 and on['sharpe'] > off['sharpe'])
    dd_down = on['max_dd'] <= off['max_dd']
    if sharpe_up and dd_down:
        print(f"  → ON improves Sharpe ({_f(off['sharpe'])}→{_f(on['sharpe'])}) at "
              f"lower/equal max-DD ({off['max_dd']:.1f}→{on['max_dd']:.1f}R): a real "
              f"variance win — keep it.")
    else:
        print(f"  → ON does NOT clearly improve risk-adjusted return "
              f"(Sharpe {_f(off['sharpe'])}→{_f(on['sharpe'])}, "
              f"maxDD {off['max_dd']:.1f}→{on['max_dd']:.1f}R). "
              f"Leaning: not worth it.")
    print()


if __name__ == "__main__":
    main()
