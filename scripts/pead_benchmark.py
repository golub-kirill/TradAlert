#!/usr/bin/env python3
"""
PEAD bar-2 diagnostic: does PEAD-ON improve the benchmark-relative picture vs SPY
(the Phase-1 bar the base strategy FAILED)?

Runs the breakeven-1.0R leg twice on the pinned snapshot — pead OFF (the base =
run_id=15-equivalent) and pead ON — and computes per-window benchmark-relative
metrics (IR at 1R=1% equity, own-Sharpe vs SPY-own-Sharpe, %-beating) for each,
then the delta. INFORMATIVE / beyond the PEAD-2 gate (which already failed Bar 1
on maxDD) — used to decide whether a drawdown-refined PEAD-3 is worth pursuing.

    .venv/Scripts/python.exe scripts/pead_benchmark.py [--snapshot data/snapshot_2026-06-10]
"""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from backtest.benchmark_metrics import (  # noqa: E402
    align_strategy_benchmark, information_ratio, pct_periods_beating,
)
from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.stats import compute_stats        # noqa: E402
from backtest.stats_utils import sharpe_ratio    # noqa: E402

from paired_ab import _run  # noqa: E402
from benchmark_relative import (  # noqa: E402
    _spy_monthly_returns, _window_mask, WINDOWS, RISK_BASE,
)


def _metrics(ec, spy_monthly):
    """Per-window benchmark-relative metrics for one leg's monthly R series."""
    periods, strat_R, spy_pct = align_strategy_benchmark(ec.monthly, spy_monthly)
    out = {}
    for label, months in WINDOWS:
        m = _window_mask(periods, months)
        sR, sP = strat_R[m], spy_pct[m]
        if len(sR) < 2:
            continue
        sPct = sR * RISK_BASE
        out[label] = dict(
            own=sharpe_ratio(sR.tolist()),
            spy=sharpe_ratio(sP.tolist()),
            ir=information_ratio(sPct, sP),
            beat=pct_periods_beating(sPct, sP),
        )
    return out, len(periods)


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
    if uni.spy_df is None:
        print("  ✗ SPY not loaded — cannot run benchmark-relative.")
        return
    spy_monthly = _spy_monthly_returns(uni.spy_df)

    cfg_off = base
    cfg_on = copy.deepcopy(base)
    cfg_on.setdefault("signals", {}).setdefault("pead", {})["enabled"] = True

    tr_off = _run(uni, cfg_off, settings, breakeven_trigger_r=1.0)
    tr_on = _run(uni, cfg_on, settings, breakeven_trigger_r=1.0)
    ec_off, ec_on = build_curve(tr_off), build_curve(tr_on)
    st_off, st_on = compute_stats(tr_off), compute_stats(tr_on)
    print(f"  pead OFF: {st_off.trades_count}t · {ec_off.total_r:+.2f}R · Sharpe {ec_off.sharpe:.2f}", flush=True)
    print(f"  pead ON : {st_on.trades_count}t · {ec_on.total_r:+.2f}R · Sharpe {ec_on.sharpe:.2f}", flush=True)

    m_off, n = _metrics(ec_off, spy_monthly)
    m_on, _ = _metrics(ec_on, spy_monthly)

    print(f"\n  Benchmark-relative vs SPY ({n} aligned months; 1R = 1% equity)")
    print("  " + "─" * 86)
    print(f"  {'window':<6} | {'base IR':>8} {'on IR':>7} {'ΔIR':>7} | "
          f"{'base ΔSh':>8} {'on ΔSh':>7} | {'base beat':>9} {'on beat':>8}")
    print("  " + "─" * 86)
    for label, _ in WINDOWS:
        if label not in m_off or label not in m_on:
            continue
        b, o = m_off[label], m_on[label]
        b_dsh, o_dsh = b["own"] - b["spy"], o["own"] - o["spy"]
        print(f"  {label:<6} | {b['ir']:>+8.3f} {o['ir']:>+7.3f} {o['ir'] - b['ir']:>+7.3f} | "
              f"{b_dsh:>+8.3f} {o_dsh:>+7.3f} | {b['beat']:>9.1%} {o['beat']:>8.1%}")

    bf, of_ = m_off["Full"], m_on["Full"]
    print("  " + "─" * 86)
    print(f"  SPY own-Sharpe (Full) = {bf['spy']:.3f}")
    print(f"  bar-2 reads (full): ΔIR = {of_['ir'] - bf['ir']:+.3f} (bar ≥ +0.05)  ·  "
          f"on own-Sharpe = {of_['own']:.3f} vs SPY {of_['spy']:.3f} "
          f"(bar: on ≥ SPY)  ·  on %beat = {of_['beat']:.1%}")
    print("  NOTE: diagnostic on the FULL-size (Bar-1-failing) config — informs whether PEAD-3 is worth it.")


if __name__ == "__main__":
    main()
