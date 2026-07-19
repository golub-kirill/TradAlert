"""
Paired A/B on a pinned data snapshot: shipped baseline vs the macro blackout
fail-safe (settings ``macro.blackout_failsafe``).

Both legs share ONE load_universe() call and the full shipped execution config
(chronic penalty + symmetric 0.002 exit slippage per the adopted conventions);
the ONLY variable is the settings flag — OFF (a total data blackout rides the
earnings_breadth placeholder to size ≈0.775) vs ON (blackout sizes at the
floor). Only paired same-snapshot deltas are meaningful.

Decision rule → docs/backtest_out/blackout_failsafe_prereg.md — INVERTED:
this is a live tail-risk fail-safe, so ADOPT requires the backtest delta to be
NEGLIGIBLE (proving it free), not positive.

Usage:
    python scripts/studies/paired_ab_blackout_failsafe.py
        [--snapshot data/snapshot_2026-06-10] [--start 2000-01-01]

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
from backtest.stress import print_stress  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402
from core.ticker_health import TickerHealth  # noqa: E402


def _run(uni, base_cfg, settings):
    exec_cfg = base_cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=float((settings.get("risk") or {}).get("max_open_risk", 5.0)),
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        exit_slippage_pct=float(exec_cfg.get("exit_slippage_pct", 0.0) or 0.0),
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
    chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
    if chronic_cfg.get("enabled"):
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
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--start", default="2000-01-01",
                    help="Reduced window = wiring smoke ONLY")
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

    settings.setdefault("macro", {})["blackout_failsafe"] = False
    settings_on = copy.deepcopy(settings)
    settings_on["macro"]["blackout_failsafe"] = True

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

    trades_base = _run(uni, base_cfg, settings)
    trades_fs = _run(uni, base_cfg, settings_on)

    print()
    print("  PAIRED A/B — one universe load, identical data both legs")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_row("baseline", trades_base))
    print(_row("blackout_failsafe", trades_fs))
    st_b, st_f = compute_stats(trades_base), compute_stats(trades_fs)
    ec_b, ec_f = build_curve(trades_base), build_curve(trades_fs)
    print("  " + "─" * 80)
    print(f"  failsafe effect: {ec_f.total_r - ec_b.total_r:+.2f}R total · "
          f"{ec_f.sharpe - ec_b.sharpe:+.3f} Sharpe · "
          f"{ec_f.sortino - ec_b.sortino:+.3f} Sortino · "
          f"{ec_f.max_dd - ec_b.max_dd:+.2f}R maxDD · "
          f"{st_f.trades_count - st_b.trades_count:+d} trades")
    if st_f.trades_count == st_b.trades_count and abs(ec_f.total_r - ec_b.total_r) < 1e-9:
        print("  NOTE: byte-identical legs — the snapshot window never hits a total "
              "data blackout; the fail-safe is FREE by construction (strongest ADOPT).")

    print_stress(trades_base, label="baseline (gate config)")

    print("\n  DECISION RULE (pre-registered, INVERTED — a fail-safe must be free):")
    print("    ADOPT iff |ΔtotalR| < 3R AND |ΔSharpe| < 0.01 AND no era worse than −2R")
    print("    CLOSE otherwise (floor too blunt; record the measured cost)\n")


if __name__ == "__main__":
    main()
