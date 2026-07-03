"""
Paired A/B on a pinned data snapshot: shipped baseline vs correlation-aware
open-risk budget (PortfolioConfig.correlation_cap).

Both legs share ONE load_universe() call — identical in-memory data by
construction — and BOTH run the real shipped execution config (breakeven,
max-hold, slippage, commission, max_open_risk). The ONLY variable is
correlation_cap: OFF (raw Σ size_mult budget) vs ON (effective risk sqrt(wᵀCw),
so correlated concurrent names share a budget slot). Only paired same-snapshot
deltas are meaningful (headline LEVELS carry ~±10R data-revision jitter).

Read the CAVEAT below before interpreting a ~0 result.

Usage:
    python scripts/studies/paired_ab_correlation.py [--snapshot data/snapshot_2026-06-10]
        [--lookback 60] [--min-overlap 40] [--floor 0.0] [--max-open-risk 5.0]

CAVEAT — why to also try a tighter --max-open-risk:
    Effective risk sqrt(wᵀCw) ≤ Σ size_mult for ρ∈[0,1], so at the SAME budget
    the cap can only ADMIT MORE diversified concurrent names (never fewer) — it
    tests "is the budget leaving diversification capacity on the table?". If the
    watchlist rarely hits the budget ceiling, the two legs are near-identical and
    the delta is ~0 (a valid answer: the cap is moot at this budget). To actually
    exercise the concentration control, re-run at a TIGHTER budget where the cap
    binds (e.g. --max-open-risk 3.0): there the correlation discount decides which
    names get the scarce slots, and the paired delta becomes informative.

Exploratory harness: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402


def _run(uni, base_cfg, settings, max_open_risk, correlation_cap=False,
         lookback=60, min_overlap=40, floor=0.0):
    """One leg. Both legs use the shipped execution config; the treatment leg
    only flips correlation_cap (+ its knobs) so the comparison is single-variable.
    """
    exec_cfg = base_cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=float(max_open_risk),
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    # Shipped breakeven stop (ADR-004) — part of the real baseline; both legs.
    be = exec_cfg.get("breakeven_trigger_r")
    if be:
        kwargs["breakeven_trigger_r"] = float(be)
        if exec_cfg.get("breakeven_buffer_atr"):
            kwargs["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])
    if correlation_cap:
        kwargs.update(
            correlation_cap=True,
            correlation_lookback_days=int(lookback),
            correlation_min_overlap=int(min_overlap),
            correlation_floor=float(floor),
        )
    pcfg = PortfolioConfig(**kwargs)
    engine = FilterEngine.from_dict(base_cfg)
    bt = PortfolioBacktester(engine, pcfg)
    t0 = time.time()
    result = bt.run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series,
        behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df,
        settings=settings,
    )
    print(f"  leg done in {time.time() - t0:.0f}s "
          f"({len(result.trades)} trades, {len(result.capped_signals)} capped)",
          flush=True)
    return result.trades


def _row(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<18} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  "
            f"{ec.sharpe:>6.2f}  {ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (with prices/ behavioral/ macro/ "
                         "earnings_history/ inside)")
    ap.add_argument("--lookback", type=int, default=60,
                    help="Correlation return window in bars (default 60)")
    ap.add_argument("--min-overlap", type=int, default=40,
                    help="Min overlapping returns to trust a pair (default 40)")
    ap.add_argument("--floor", type=float, default=0.0,
                    help="Zero out correlations below this (default 0.0)")
    ap.add_argument("--max-open-risk", type=float, default=None,
                    help="Budget for BOTH legs (default: settings.risk.max_open_risk "
                         "or 5.0). Try a tighter value so the cap binds — see CAVEAT.")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", []))
               if isinstance(t, str)]

    mor = args.max_open_risk
    if mor is None:
        mor = float((settings.get("risk") or {}).get("max_open_risk", 5.0))

    print(f"  Snapshot: {snap}", flush=True)
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
    print(f"  Budget (both legs): {mor:g}R · cap knobs: lookback={args.lookback}d "
          f"min_overlap={args.min_overlap} floor={args.floor:g}", flush=True)

    trades_base = _run(uni, base_cfg, settings, mor, correlation_cap=False)
    trades_cap = _run(uni, base_cfg, settings, mor, correlation_cap=True,
                      lookback=args.lookback, min_overlap=args.min_overlap,
                      floor=args.floor)

    print()
    print("  PAIRED A/B — one universe load, identical data both legs")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_row("baseline", trades_base))
    print(_row("correlation_cap", trades_cap))
    st_b, st_c = compute_stats(trades_base), compute_stats(trades_cap)
    ec_b, ec_c = build_curve(trades_base), build_curve(trades_cap)
    print("  " + "─" * 80)
    print(f"  cap effect: {ec_c.total_r - ec_b.total_r:+.2f}R total · "
          f"{ec_c.sharpe - ec_b.sharpe:+.3f} Sharpe · "
          f"{ec_c.sortino - ec_b.sortino:+.3f} Sortino · "
          f"{ec_c.max_dd - ec_b.max_dd:+.2f}R maxDD · "
          f"{st_c.expectancy_r - st_b.expectancy_r:+.4f} E[R] · "
          f"{st_c.trades_count - st_b.trades_count:+d} trades")
    if st_c.trades_count == st_b.trades_count:
        print("  NOTE: identical trade counts → the budget never bound this run. "
              "Re-run with a tighter --max-open-risk so the cap is exercised.")


if __name__ == "__main__":
    main()
