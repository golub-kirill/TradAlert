#!/usr/bin/env python3
"""
Paired A/B for the PEAD signal: the breakeven-1.0R headline with PEAD off vs on.

ONE load_universe() call → identical in-memory data for both legs (prices from the
pinned snapshot; earnings events from data/earnings_history_pead/). Leg A = the
+113.58R headline (pead off, the byte-identical regression target); Leg B = the SAME
config plus signals.pead.enabled. Reports the paired deltas + the PEAD sleeve's own
stats (signal_type == 'pead'). Exploratory harness — no journal, no HTML, no CSV.

    .venv/Scripts/python.exe scripts/pead_ab.py [--snapshot data/snapshot_2026-06-10]
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from datetime import date
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


def _run(uni, cfg, settings):
    exec_cfg = cfg.get("execution", {})
    pcfg = PortfolioConfig(
        max_open_risk=5.0,
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
        breakeven_trigger_r=1.0,
    )
    engine = FilterEngine.from_dict(cfg)
    bt = PortfolioBacktester(engine, pcfg)
    t0 = time.time()
    res = bt.run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series,
        behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df,
        settings=settings,
    )
    print(f"  leg done in {time.time() - t0:.0f}s", flush=True)
    return res.trades


def _line(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<18} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  {ec.sharpe:>6.2f}  "
            f"{ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


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

    base = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", []) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral",
        start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)
    n_tk = sum(1 for p in uni.prepped.values() if p.earnings_events)
    n_ev = sum(len(p.earnings_events) for p in uni.prepped.values())
    print(f"  PEAD events: {n_ev} across {n_tk} tickers", flush=True)

    cfg_off = base
    cfg_on = copy.deepcopy(base)
    cfg_on.setdefault("signals", {}).setdefault("pead", {})["enabled"] = True

    trades_off = _run(uni, cfg_off, settings)
    trades_on = _run(uni, cfg_on, settings)

    print("\n  PEAD A/B — one universe load, identical data both legs (breakeven 1.0R)")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_line("pead OFF (base)", trades_off))
    print(_line("pead ON", trades_on))

    ec_off, ec_on = build_curve(trades_off), build_curve(trades_on)
    st_off, st_on = compute_stats(trades_off), compute_stats(trades_on)
    print("  " + "─" * 80)
    print(f"  PEAD effect: {ec_on.total_r - ec_off.total_r:+.2f}R · "
          f"{ec_on.sharpe - ec_off.sharpe:+.3f} Sharpe · "
          f"{ec_on.sortino - ec_off.sortino:+.3f} Sortino · "
          f"{ec_on.max_dd - ec_off.max_dd:+.2f}R maxDD · "
          f"{st_on.expectancy_r - st_off.expectancy_r:+.4f} E[R] · "
          f"{st_on.trades_count - st_off.trades_count:+d} trades")

    pead_trades = [t for t in trades_on if getattr(t, "signal_type", "") == "pead"]
    print("  " + "─" * 80)
    if pead_trades:
        st_p = compute_stats(pead_trades)
        ec_p = build_curve(pead_trades)
        print(f"  PEAD sleeve only: {st_p.trades_count} trades · WR {st_p.win_rate:.1%} · "
              f"E[R] {st_p.expectancy_r:+.3f} · totalR {ec_p.total_r:+.2f}")
    else:
        print("  PEAD sleeve: 0 pead-tagged trades (check wiring / events)")

    print("  " + "─" * 80)
    print("  Byte-identical check: 'pead OFF' must reproduce the +113.58R / 1635t / 0.57 headline.")


if __name__ == "__main__":
    main()
