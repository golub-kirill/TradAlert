"""
Oracle-ceiling measurement for the budget-fill ranking seam.

Question: on days when more signals fire than the open-risk budget admits,
could ANY ranking — perfect foresight included — have picked better entries
than the current insertion-order fill? The answer upper-bounds the value of
every possible scoring layer at the only decision point a score influences
when scoring is off (the budget tiebreak in portfolio_backtester).

Method
------
Run A: shipped config (budget 5.0)      -> fills per day + budget-capped signals.
Run B: budget 10_000 (unconstrained)    -> realized R for every would-be entry,
       i.e. the counterfactual outcome of run A's capped signals.
Per day D with K = run A fills: candidates = A-fills(D) + A-capped(D) that have
a (ticker, entry_date) match in run B. The oracle keeps the top-K candidates by
effective R (r_multiple x size_mult). Ceiling = sum over days of
(oracle picks - actual picks).

This is an upper bound by construction: the oracle sees realized R exactly and
the count-K capacity proxy ignores multi-day budget occupancy knock-ons. If
even this ceiling is small, no scorer can add meaningful R here.

Exploratory harness: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from core.filter_engine import FilterEngine  # noqa: E402


def _run(uni, base_cfg, settings, max_open_risk: float):
    exec_cfg = base_cfg.get("execution", {})
    pcfg = PortfolioConfig(
        max_open_risk=max_open_risk,
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
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
    print(f"  budget={max_open_risk:g}: {len(result.trades)} trades, "
          f"{len(result.capped_signals)} capped, {time.time() - t0:.0f}s",
          flush=True)
    return result


def _eff_r(t) -> float:
    return float(t.r_multiple or 0.0) * float(t.size_mult or 1.0)


def main() -> None:
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", []))
               if isinstance(t, str)]

    from datetime import date
    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=_ROOT / "data" / "prices",
        earnings_dir=_ROOT / "data" / "earnings_history",
        start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)

    res_a = _run(uni, base_cfg, settings, max_open_risk=5.0)
    res_b = _run(uni, base_cfg, settings, max_open_risk=10_000.0)

    # Counterfactual outcomes: every would-be entry in the unconstrained run.
    cf = {(t.ticker, t.entry_date): _eff_r(t) for t in res_b.trades}

    fills_by_day: dict = defaultdict(list)
    for t in res_a.trades:
        fills_by_day[t.entry_date].append(_eff_r(t))

    capped_by_day: dict = defaultdict(list)
    n_mult0 = n_unmatched = n_matched = 0
    for c in res_a.capped_signals:
        if float(getattr(c.signal, "size_mult", 1.0) or 0.0) <= 0:
            n_mult0 += 1          # excluded regardless of order — not rankable
            continue
        key = (c.ticker, c.date)
        if key in cf:
            capped_by_day[c.date].append(cf[key])
            n_matched += 1
        else:
            n_unmatched += 1      # path-divergence: no counterfactual; skipped

    total_actual = total_oracle = 0.0
    bind_days = []
    for day, capped in capped_by_day.items():
        fills = fills_by_day.get(day, [])
        k = len(fills)
        if k == 0:
            continue              # dd-gate / nothing filled: oracle fills nothing too
        candidates = sorted(fills + capped, reverse=True)
        oracle = sum(candidates[:k])
        actual = sum(fills)
        total_actual += actual
        total_oracle += oracle
        bind_days.append((day, oracle - actual, k, len(capped)))

    total_r_a = sum(_eff_r(t) for t in res_a.trades)
    total_r_b = sum(_eff_r(t) for t in res_b.trades)
    years = max(1.0, len(fills_by_day) and
                ((max(fills_by_day) - min(fills_by_day)).days / 365.25))
    delta = total_oracle - total_actual

    print()
    print("=" * 68)
    print("  ORACLE CEILING — budget-fill ranking seam")
    print("=" * 68)
    print(f"  Run A (budget 5.0)   : total {total_r_a:+.1f}R over {len(res_a.trades)} trades")
    print(f"  Run B (unconstrained): total {total_r_b:+.1f}R over {len(res_b.trades)} trades")
    print(f"  Capped signals       : {len(res_a.capped_signals)} "
          f"(mult-0 {n_mult0}, matched {n_matched}, unmatched {n_unmatched})")
    print(f"  Bind days (K>0)      : {len(bind_days)} days with a real ranking choice")
    print(f"  Actual R on bind days: {total_actual:+.1f}R")
    print(f"  Oracle R on bind days: {total_oracle:+.1f}R")
    print(f"  ── ORACLE CEILING    : {delta:+.1f}R total  "
          f"({delta / years:+.2f}R/yr over {years:.0f}y) ──")
    if total_r_a:
        print(f"  As share of total    : {100 * delta / abs(total_r_a):.1f}% of run-A R")
    print()
    top = sorted(bind_days, key=lambda x: -x[1])[:10]
    print("  Top bind days (date, oracle-gain, fills, capped):")
    for day, gain, k, ncap in top:
        print(f"    {day}  {gain:+6.2f}R  K={k}  capped={ncap}")


if __name__ == "__main__":
    main()
