"""Paired A/B: held-long regime-flip exit tactics on one pinned snapshot.

Compares the current "exit on any non-BULL bar" against the two shaping levers
(signals.exits.regime_flip_bear_only, regime_flip_confirm_bars) added for this
study. All legs share ONE load_universe() call, so the only thing that varies is
the exit config — deltas are clean and reproducible on the frozen snapshot.

Usage:
    python scripts/studies/regime_exit_ab.py [--snapshot data/snapshot_2026-06-10]

Exploratory: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

# universe.summary() carries a "→"; force UTF-8 so a redirected stdout on Windows
# (cp1252) doesn't crash the run.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402

# (label, exits-override dict merged onto the shipped signals.exits block)
_LEGS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("bear_only", {"regime_flip_bear_only": True}),
    ("confirm_2", {"regime_flip_confirm_bars": 2}),
    ("confirm_3", {"regime_flip_confirm_bars": 3}),
]


def _run(uni, base_cfg, settings, exits_over: dict):
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("signals", {}).setdefault("exits", {})
    cfg["signals"]["exits"].update(exits_over)
    exec_cfg = base_cfg.get("execution", {})
    pcfg = PortfolioConfig(
        max_open_risk=5.0,
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    engine = FilterEngine.from_dict(cfg)
    bt = PortfolioBacktester(engine, pcfg)
    t0 = time.time()
    result = bt.run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series,
        behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df,
        settings=settings,
    )
    print(f"  {'leg done':<16} in {time.time() - t0:.0f}s", flush=True)
    return result.trades


def _row(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<16} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  "
            f"{ec.sharpe:>6.2f}  {ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]

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

    results = {label: _run(uni, base_cfg, settings, over) for label, over in _LEGS}

    print()
    print("  PAIRED A/B — regime-flip exit tactics, one universe load")
    print("  " + "─" * 78)
    print(f"  {'config':<16} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    for label, _ in _LEGS:
        print(_row(label, results[label]))

    ec_base = build_curve(results["baseline"])
    st_base = compute_stats(results["baseline"])
    print("  " + "─" * 78)
    print("  Δ vs baseline (positive totalR/Sharpe = better):")
    for label, _ in _LEGS[1:]:
        ec = build_curve(results[label])
        st = compute_stats(results[label])
        print(f"    {label:<14} {ec.total_r - ec_base.total_r:+8.2f}R  "
              f"{ec.sharpe - ec_base.sharpe:+.3f} Sharpe  "
              f"{ec.sortino - ec_base.sortino:+.3f} Sortino  "
              f"{ec.max_dd - ec_base.max_dd:+.2f}R maxDD  "
              f"{st.expectancy_r - st_base.expectancy_r:+.4f} E[R]  "
              f"{st.trades_count - st_base.trades_count:+d} trades")


if __name__ == "__main__":
    main()
