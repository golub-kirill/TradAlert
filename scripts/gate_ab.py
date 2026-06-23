#!/usr/bin/env python3
"""
Audit-gate paired A/B (risk discipline), borrow-honest, vs SPY — two modes:

  --mode event_block : inject TradingView FOMC+CPI+NFP US dates (2015-2026) into
                       events.stop_dates so a fresh entry on an event day is blocked
                       (the audit's S6 gate, real non-fabricated dates).
  --mode vix_slope   : set regime.vix_slope_block=true (block fresh momentum entries
                       when VIX is rising even at a low level — the audit's S4).

Pre-registered: docs/backtest_out/audit_gates_prereg.md. Two paired legs on ONE snapshot
load (OFF == COT-only headline / byte-identical baseline; ON == the gate enabled).
Bars: gate-in (>=20 entries blocked), Bar1 (maxDD down + Sharpe not worse), Bar2 (E[R]
not worse), Bar3 (excess-Sharpe vs SPY not worse, {0.5,1,2}% band). Default OFF until a
separate DSR/White's-RC clears. Exploratory: no journal/HTML/CSV.

    .venv/Scripts/python.exe scripts/gate_ab.py --mode event_block
    .venv/Scripts/python.exe scripts/gate_ab.py --mode vix_slope
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

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402
import yaml          # noqa: E402

from backtest.benchmark_metrics import (  # noqa: E402
    align_strategy_benchmark, information_ratio, month_end_returns,
)
from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.stats import compute_stats        # noqa: E402

from paired_ab import _run  # noqa: E402

RISK_BAND = [0.005, 0.010, 0.020]
RISK_BASE = 0.010
MIN_BLOCKED = 20


def _excess(monthly, spy_monthly, alpha):
    if monthly is None or len(monthly) < 2:
        return float("nan")
    per, sR, sP = align_strategy_benchmark(monthly, spy_monthly)
    if len(per) < 2:
        return float("nan")
    return information_ratio(sR * alpha, sP)


def _event_stop_dates() -> list[dict]:
    """TradingView FOMC+CPI+NFP US dates → events.stop_dates entries (cached, fail-open)."""
    from core.fetchers.macro.tv_calendar import fetch_tv_calendar
    df = fetch_tv_calendar("2015-01-01", "2026-12-31", countries=("US",),
                           cache_dir=_ROOT / "data" / "macro")
    out = []
    for i, r in enumerate(df.itertuples(), start=9000):
        out.append({"id": i, "date": pd.Timestamp(r.date).date().isoformat(),
                    "description": f"{r.category} (TV)"})
    return out


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Audit-gate paired A/B (vs SPY)")
    ap.add_argument("--mode", required=True, choices=["event_block", "vix_slope"])
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--tickers", nargs="+", default=None, help="smoke-test subset")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = args.tickers or [t for t in wl.get("tier_a", wl.get("tickers", []))
                               if isinstance(t, str)]

    print(f"  Snapshot: {snap}  ·  mode={args.mode}", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True, cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history", macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral", start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)
    if uni.spy_df is None:
        print("  ✗ SPY not loaded — cannot run the vs-SPY bar.")
        return

    cfg_off = copy.deepcopy(base_cfg)
    cfg_on = copy.deepcopy(base_cfg)
    if args.mode == "event_block":
        ev = _event_stop_dates()
        print(f"  injecting {len(ev)} TV event dates into stop_dates (ON leg)", flush=True)
        cfg_on.setdefault("events", {}).setdefault("stop_dates", [])
        cfg_on["events"]["stop_dates"] = list(cfg_on["events"]["stop_dates"]) + ev
    else:  # vix_slope
        cfg_on.setdefault("regime", {})["vix_slope_block"] = True

    print("  Running OFF leg (== headline)…", flush=True)
    trades_off = _run(uni, cfg_off, settings, breakeven_trigger_r=1.0)
    print(f"  Running ON leg ({args.mode})…", flush=True)
    trades_on = _run(uni, cfg_on, settings, breakeven_trigger_r=1.0)

    st_off, st_on = compute_stats(trades_off), compute_stats(trades_on)
    ec_off, ec_on = build_curve(trades_off), build_curve(trades_on)
    spy_monthly = month_end_returns(uni.spy_df["close"])
    ex_off = {a: _excess(ec_off.monthly, spy_monthly, a) for a in RISK_BAND}
    ex_on = {a: _excess(ec_on.monthly, spy_monthly, a) for a in RISK_BAND}

    blocked = st_off.trades_count - st_on.trades_count
    bar = "  " + "─" * 76
    print("\n" + "=" * 80)
    print(f"  AUDIT-GATE A/B — mode={args.mode} (snapshot)")
    print("=" * 80)
    print(f"  {'leg':<10} {'trades':>6} {'WR':>6} {'E[R]':>7} {'totalR':>8} "
          f"{'Sharpe':>7} {'Sortino':>7} {'maxDD':>7}")
    print(bar)
    for lbl, st, ec in (("OFF", st_off, ec_off), ("ON", st_on, ec_on)):
        print(f"  {lbl:<10} {st.trades_count:>6} {st.win_rate:>6.1%} {st.expectancy_r:>+7.3f} "
              f"{ec.total_r:>+8.2f} {ec.sharpe:>7.2f} {ec.sortino:>7.2f} {ec.max_dd:>7.2f}")
    print(bar)
    print(f"  blocked {blocked} entries · ΔtotalR {ec_on.total_r - ec_off.total_r:+.2f} · "
          f"ΔSharpe {ec_on.sharpe - ec_off.sharpe:+.3f} · ΔmaxDD {ec_on.max_dd - ec_off.max_dd:+.2f} · "
          f"ΔE[R] {st_on.expectancy_r - st_off.expectancy_r:+.4f}")
    print("  excess-Sharpe vs SPY: " + " · ".join(
        f"{a*100:g}% OFF {ex_off[a]:+.2f}/ON {ex_on[a]:+.2f}" for a in RISK_BAND))

    gate = blocked >= MIN_BLOCKED
    bar1 = (ec_on.max_dd <= ec_off.max_dd) and (ec_on.sharpe >= ec_off.sharpe - 0.02)
    bar2 = st_on.expectancy_r >= st_off.expectancy_r
    bar3 = all(ex_on[a] >= ex_off[a] for a in RISK_BAND)
    ship = gate and bar1 and bar2 and bar3

    def _m(b): return "PASS" if b else "FAIL"
    print("\n" + "=" * 80)
    print("  PRE-REGISTERED VERDICT (audit_gates_prereg.md — FROZEN)")
    print(bar)
    print(f"    Gate-in : blocked {blocked} (need ≥{MIN_BLOCKED})        [{'PASS' if gate else 'INACTIVE'}]")
    print(f"    Bar 1   : maxDD↓ & Sharpe not worse                 [{_m(bar1)}]")
    print(f"    Bar 2   : E[R](ON) ≥ E[R](OFF)                      [{_m(bar2)}]")
    print(f"    Bar 3   : excess-Sharpe vs SPY ON≥OFF (base+band)   [{_m(bar3)}]")
    print(bar)
    if not gate:
        print(f"    >>> VERDICT: INACTIVE — gate barely fires; do not ship. <<<")
    elif ship:
        print(f"    >>> VERDICT: PROMISING — clears the paired bars. NEXT: deflated-Sharpe +")
        print(f"        White's RC (owner terminal) before enabling. <<<")
    else:
        print(f"    >>> VERDICT: DO NOT SHIP — ≥1 bar FAILed. Gate stays OFF (honest result). <<<")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
