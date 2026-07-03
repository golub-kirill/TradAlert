#!/usr/bin/env python3
"""
Textbook check — does the strategy beat buy-and-hold SPY on a risk-adjusted basis?

Computes passive SPY buy-and-hold metrics over several windows (full history +
trailing 10y/5y/3y/1y) and compares the risk-adjusted ratios to the strategy
headline. The strategy is measured in R (scale-invariant) and SPY in %, but
**Sharpe / Sortino / Calmar are unit-less ratios**, so they compare directly —
that is the honest "does the edge beat the passive baseline" test (NORTH STAR #1).

Sharpe/Sortino use the same convention as the backtester (`stats_utils`,
monthly series, rf=0, annualised by √12). Read-only on `data/prices/SPY.parquet`;
no DB, no sweep workers — safe to run anytime.

    python scripts/studies/benchmark_spy.py
    python scripts/studies/benchmark_spy.py --ticker SPY --strategy-sharpe 0.66
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def cagr(close, years: float) -> float:
    """Compound annual growth rate from first→last close over `years`."""
    if years <= 0 or len(close) < 2 or close.iloc[0] <= 0:
        return float("nan")
    return float((close.iloc[-1] / close.iloc[0]) ** (1.0 / years) - 1.0)


def max_drawdown_pct(close) -> float:
    """Peak-to-trough drawdown of a compounded price series, as a positive %."""
    import numpy as np
    c = np.asarray(close, dtype=float)
    if len(c) < 2:
        return 0.0
    peak = np.maximum.accumulate(c)
    return float((1.0 - c / peak).max())


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import pandas as pd
    from backtest.stats_utils import sharpe_ratio, sortino_ratio

    ap = argparse.ArgumentParser(description="Strategy vs buy-and-hold SPY (risk-adjusted)")
    ap.add_argument("--ticker", default="SPY", help="Benchmark ticker (default SPY)")
    ap.add_argument("--strategy-sharpe", type=float, default=0.66,
                    help="Strategy headline annualised Sharpe to compare against "
                         "(default 0.66 = run_id=11).")
    args = ap.parse_args()

    path = _ROOT / "data" / "prices" / f"{args.ticker.upper()}.parquet"
    if not path.exists():
        print(f"  ✗ {path} not found — fetch the price cache first.")
        return

    df = pd.read_parquet(path).sort_index()
    df.index = pd.to_datetime(df.index)
    close = df["close"].dropna()
    if len(close) < 60:
        print(f"  ✗ only {len(close)} bars for {args.ticker} — not enough history.")
        return

    monthly_close = close.resample("ME").last().dropna()
    monthly_ret = monthly_close.pct_change().dropna()

    last = close.index[-1]
    windows = [
        ("Full", None),
        ("10y", pd.DateOffset(years=10)),
        ("5y", pd.DateOffset(years=5)),
        ("3y", pd.DateOffset(years=3)),
        ("1y", pd.DateOffset(years=1)),
    ]

    print("\n" + "=" * 74)
    print(f"  Textbook check — strategy vs buy-and-hold {args.ticker.upper()}")
    print(f"  {args.ticker.upper()} history: {close.index[0]:%Y-%m-%d} → {last:%Y-%m-%d}  "
          f"({len(close)} bars)")
    print("  " + "-" * 70)
    print(f"  {'Window':>6} | {'CAGR':>7} | {'Sharpe':>7} | {'Sortino':>7} | "
          f"{'MaxDD':>7} | {'Calmar':>7}")
    print("  " + "-" * 70)

    spy_full_sharpe = float("nan")
    for label, off in windows:
        start = (last - off) if off is not None else close.index[0]
        c = close[close.index >= start]
        mr = monthly_ret[monthly_ret.index >= start]
        if len(c) < 30 or len(mr) < 2:
            continue
        yrs = (c.index[-1] - c.index[0]).days / 365.25
        g = cagr(c, yrs)
        sh = sharpe_ratio(mr.tolist())
        so = sortino_ratio(mr.tolist())
        dd = max_drawdown_pct(c)
        cal = (g / dd) if dd > 0 else float("inf")
        if label == "Full":
            spy_full_sharpe = sh
        so_str = "  inf" if so == float("inf") else f"{so:6.2f}"
        cal_str = "  inf" if cal == float("inf") else f"{cal:6.2f}"
        print(f"  {label:>6} | {g * 100:6.1f}% | {sh:7.2f} | {so_str} | "
              f"{dd * 100:6.1f}% | {cal_str}")

    print("  " + "-" * 70)
    print(f"  Strategy headline (annualised Sharpe) : {args.strategy_sharpe:.2f}")
    print(f"  {args.ticker.upper()} buy-and-hold (full-window Sharpe): {spy_full_sharpe:.2f}")
    if spy_full_sharpe == spy_full_sharpe:  # not NaN
        verdict = ("strategy BEATS passive on risk-adjusted return"
                   if args.strategy_sharpe > spy_full_sharpe
                   else "strategy does NOT beat passive on Sharpe")
        print(f"  Verdict: {verdict} "
              f"(Δ {args.strategy_sharpe - spy_full_sharpe:+.2f} Sharpe)")
    print("  Note: Sharpe is scale-invariant (rf=0, monthly, ×√12), so the R-based")
    print("        strategy and %-based SPY compare directly on this axis. Absolute")
    print("        return is not comparable (different units / leverage).")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
