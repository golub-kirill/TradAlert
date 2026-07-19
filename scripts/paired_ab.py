"""
Paired A/B on a pinned data snapshot: baseline vs breakeven-1.0R.

Both legs share ONE load_universe() call — identical in-memory data by
construction, immune to any cache refresh mid-experiment. Run against a
frozen copy of the caches (data/snapshot_<date>/) so the comparison is also
reproducible later; headline LEVELS carry ~±10R day-over-day data-revision
jitter (full-history price re-adjustment), so only paired same-snapshot
deltas are meaningful.

Usage:
    python scripts/paired_ab.py [--snapshot data/snapshot_2026-06-10]

Exploratory harness: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
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


def _run(uni, base_cfg, settings, breakeven_trigger_r=None):
    exec_cfg = base_cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=5.0,
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    if breakeven_trigger_r is not None:
        kwargs["breakeven_trigger_r"] = float(breakeven_trigger_r)
    # Chronic-loser penalty follows the production YAML switch (ADOPTED
    # 2026-07-17, D-011) in BOTH legs — the A/B keeps isolating the BE lever.
    # Fresh tracker per leg: it is stateful and must never be shared.
    chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
    if chronic_cfg.get("enabled"):
        from core.ticker_health import TickerHealth
        kwargs["ticker_health"] = TickerHealth.from_config(chronic_cfg)
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
    print(f"  leg done in {time.time() - t0:.0f}s", flush=True)
    return result.trades


def _row(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<16} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  "
            f"{ec.sharpe:>6.2f}  {ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (with prices/ behavioral/ macro/ "
                         "earnings_history/ inside)")
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

    from datetime import date
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

    trades_base = _run(uni, base_cfg, settings)
    trades_be = _run(uni, base_cfg, settings, breakeven_trigger_r=1.0)

    print()
    print("  PAIRED A/B — one universe load, identical data both legs")
    print("  " + "─" * 76)
    print(f"  {'config':<16} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_row("baseline", trades_base))
    print(_row("breakeven 1.0R", trades_be))
    st_b, st_e = compute_stats(trades_base), compute_stats(trades_be)
    ec_b, ec_e = build_curve(trades_base), build_curve(trades_be)
    print("  " + "─" * 76)
    print(f"  BE effect: {ec_e.total_r - ec_b.total_r:+.2f}R total · "
          f"{ec_e.sharpe - ec_b.sharpe:+.3f} Sharpe · "
          f"{ec_e.sortino - ec_b.sortino:+.3f} Sortino · "
          f"{ec_e.max_dd - ec_b.max_dd:+.2f}R maxDD · "
          f"{st_e.expectancy_r - st_b.expectancy_r:+.4f} E[R]")


if __name__ == "__main__":
    main()
