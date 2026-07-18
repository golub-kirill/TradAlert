"""
Paired A/B on a pinned data snapshot: shipped baseline vs the two-sided
earnings buffer (events.earnings_buffer_two_sided).

Both legs share ONE load_universe() call and the full shipped execution config;
the ONLY variable is the flag — OFF (forward-only buffer, baseline) vs ON (also
block entries within the buffer AFTER the last earnings). The backtester
threads prev-earnings from the same per-ticker history the forward arm uses,
computed only when the flag is on. Only paired same-snapshot deltas are
meaningful.

Decision rule → docs/backtest_out/two_sided_earnings_prereg.md (fixed BEFORE
the full-window run).

Usage:
    python scripts/studies/paired_ab_two_sided_earnings.py
        [--snapshot data/snapshot_2026-06-10] [--start 2000-01-01]

--start exists for wiring smokes on a reduced window; the pre-registered
decision reads the full-window run only.

Exploratory harness: no journal, no HTML, no CSV.
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

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402

_ERAS = ((2000, 2010), (2011, 2017), (2018, 2026))


def _run(uni, cfg, settings):
    """One leg. The treatment leg differs ONLY in events.earnings_buffer_two_sided."""
    exec_cfg = cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=float((settings.get("risk") or {}).get("max_open_risk", 5.0)),
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    be = exec_cfg.get("breakeven_trigger_r")
    if be:
        kwargs["breakeven_trigger_r"] = float(be)
        if exec_cfg.get("breakeven_buffer_atr"):
            kwargs["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])
    pcfg = PortfolioConfig(**kwargs)
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


def _era_table(trades_base, trades_two):
    print("\n  ERA STABILITY (paired deltas; a one-era carry fails the rule)")
    print(f"  {'era':<12} {'base t':>7} {'2side t':>7} {'base R':>9} {'2side R':>9} "
          f"{'ΔR':>8} {'base SR':>8} {'2side SR':>8} {'ΔSR':>7}")
    for lo, hi in _ERAS:
        b = [t for t in trades_base if lo <= t.entry_date.year <= hi]
        p = [t for t in trades_two if lo <= t.entry_date.year <= hi]
        if not b and not p:
            continue
        ecb, ecp = build_curve(b), build_curve(p)
        print(f"  {f'{lo}-{hi}':<12} {len(b):>7} {len(p):>7} "
              f"{ecb.total_r:>+9.2f} {ecp.total_r:>+9.2f} "
              f"{ecp.total_r - ecb.total_r:>+8.2f} "
              f"{ecb.sharpe:>8.2f} {ecp.sharpe:>8.2f} "
              f"{ecp.sharpe - ecb.sharpe:>+7.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (with prices/ behavioral/ macro/ "
                         "earnings_history/ inside)")
    ap.add_argument("--start", default="2000-01-01",
                    help="Backtest start (reduced window = wiring smoke ONLY; "
                         "the pre-registered decision reads the full window)")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot
    start = date.fromisoformat(args.start)

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", []))
               if isinstance(t, str)]

    base_cfg.setdefault("events", {})["earnings_buffer_two_sided"] = False
    two_cfg = copy.deepcopy(base_cfg)
    two_cfg["events"]["earnings_buffer_two_sided"] = True

    print(f"  Snapshot: {snap}", flush=True)
    if start != date(2000, 1, 1):
        print(f"  REDUCED WINDOW from {start} — wiring smoke, NOT the decision run",
              flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral",
        start_date=start,
    )
    print(f"  {uni.summary()}", flush=True)
    buf = (base_cfg.get("events") or {}).get("earnings_buffer_days", 5)
    print(f"  Buffer both arms: {buf}d · treatment adds the trailing arm", flush=True)

    trades_base = _run(uni, base_cfg, settings)
    trades_two = _run(uni, two_cfg, settings)

    print()
    print("  PAIRED A/B — one universe load, identical data both legs")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_row("baseline", trades_base))
    print(_row("two_sided", trades_two))
    st_b, st_t = compute_stats(trades_base), compute_stats(trades_two)
    ec_b, ec_t = build_curve(trades_base), build_curve(trades_two)
    print("  " + "─" * 80)
    print(f"  two-sided effect: {ec_t.total_r - ec_b.total_r:+.2f}R total · "
          f"{ec_t.sharpe - ec_b.sharpe:+.3f} Sharpe · "
          f"{ec_t.sortino - ec_b.sortino:+.3f} Sortino · "
          f"{ec_t.max_dd - ec_b.max_dd:+.2f}R maxDD · "
          f"{st_t.expectancy_r - st_b.expectancy_r:+.4f} E[R] · "
          f"{st_t.trades_count - st_b.trades_count:+d} trades")

    _era_table(trades_base, trades_two)

    print("\n  DECISION RULE (pre-registered, docs/backtest_out/two_sided_earnings_prereg.md):")
    print("    ADOPT  iff ΔSharpe ≥ +0.02 AND ΔmaxDD ≤ +0.5R AND ΔSR ≥ 0 in ≥ 2 of 3 eras")
    print("    CLOSE  otherwise (keep OFF; right-tail tax or negligible)")
    print("    NOTE   adoption changes BOTH paths → re-baseline + a Phase-5 boundary\n")


if __name__ == "__main__":
    main()
