"""
Paired A/B on a pinned data snapshot: shipped baseline vs the chronic-loser
size penalty (PortfolioConfig.ticker_health, scale from filters.yaml).

Both legs share ONE load_universe() call — identical in-memory data by
construction — and BOTH run the real shipped execution config (breakeven,
max-hold, slippage, commission, max_open_risk). The ONLY variable is the
tracker: None (baseline) vs TickerHealth on the shipped scale (2 losses in the
lookback → 0.5×, 3+ → 0.25×, never a hard block). A fresh tracker is built
inside the treatment leg — it is stateful and must never be shared or reused.
Only paired same-snapshot deltas are meaningful.

Decision rule → docs/backtest_out/chronic_loser_prereg.md (fixed BEFORE the
full-window run; adopt/delete/keep-off).

Usage:
    python scripts/studies/paired_ab_chronic_loser.py
        [--snapshot data/snapshot_2026-06-10] [--start 2000-01-01]

--start exists for wiring smokes on a reduced window; the pre-registered
decision reads the full-window run only.

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
from core.ticker_health import TickerHealth  # noqa: E402

# Era folds shared with the liquidity-edge study — an aggregate win carried by
# a single era must be visible (the regime-flip-exit trap).
_ERAS = ((2000, 2010), (2011, 2017), (2018, 2026))


def _run(uni, base_cfg, settings, *, chronic: bool):
    """One leg. The treatment leg only attaches a fresh shipped-scale tracker."""
    exec_cfg = base_cfg.get("execution", {})
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
    if chronic:
        chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
        kwargs["ticker_health"] = TickerHealth.from_config(
            {**chronic_cfg, "enabled": True})
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


def _era_table(trades_base, trades_pen):
    print("\n  ERA STABILITY (paired deltas; a one-era carry fails the rule)")
    print(f"  {'era':<12} {'base t':>7} {'pen t':>7} {'base R':>9} {'pen R':>9} "
          f"{'ΔR':>8} {'base SR':>8} {'pen SR':>8} {'ΔSR':>7}")
    for lo, hi in _ERAS:
        b = [t for t in trades_base if lo <= t.entry_date.year <= hi]
        p = [t for t in trades_pen if lo <= t.entry_date.year <= hi]
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

    chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
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
    print(f"  Penalty scale: lookback={chronic_cfg.get('lookback_days', 90)}d "
          f"scale={chronic_cfg.get('scale', {2: 0.5, 3: 0.25})}", flush=True)

    trades_base = _run(uni, base_cfg, settings, chronic=False)
    trades_pen = _run(uni, base_cfg, settings, chronic=True)

    print()
    print("  PAIRED A/B — one universe load, identical data both legs")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_row("baseline", trades_base))
    print(_row("chronic_penalty", trades_pen))
    st_b, st_p = compute_stats(trades_base), compute_stats(trades_pen)
    ec_b, ec_p = build_curve(trades_base), build_curve(trades_pen)
    print("  " + "─" * 80)
    print(f"  penalty effect: {ec_p.total_r - ec_b.total_r:+.2f}R total · "
          f"{ec_p.sharpe - ec_b.sharpe:+.3f} Sharpe · "
          f"{ec_p.sortino - ec_b.sortino:+.3f} Sortino · "
          f"{ec_p.max_dd - ec_b.max_dd:+.2f}R maxDD · "
          f"{st_p.expectancy_r - st_b.expectancy_r:+.4f} E[R] · "
          f"{st_p.trades_count - st_b.trades_count:+d} trades")

    _era_table(trades_base, trades_pen)

    print("\n  DECISION RULE (pre-registered, docs/backtest_out/chronic_loser_prereg.md):")
    print("    ADOPT  iff ΔSharpe ≥ +0.02 AND ΔmaxDD ≤ +0.5R AND ΔSR ≥ 0 in ≥ 2 of 3 eras")
    print("    DELETE iff |ΔtotalR| < 3R AND |ΔSharpe| < 0.01  (inert at shipped scale)")
    print("    else   keep OFF, log CLOSED — no scale-tuning after seeing results\n")


if __name__ == "__main__":
    main()
