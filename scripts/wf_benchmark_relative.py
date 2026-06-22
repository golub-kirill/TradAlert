#!/usr/bin/env python3
"""
Walk-forward benchmark-relative truth (Phase 3) — is the SPY-relative edge stable
OUT-OF-SAMPLE, or only in-sample?

Runs the FIXED-config (no re-tune) walk-forward on the pinned snapshot — byte-identical
config to ``heavy_wf_fixed`` / the COT-only headline (positioning is COT-only via
SweepEngine's cached settings) — then, per OOS window, compares the strategy's monthly-R
to buy-and-hold SPY %-returns over that window. Reports per-window excess-Sharpe / beat?
and the POOLED OOS excess-Sharpe.

UNITS (§P2-M / §P3-M, see docs/backtest_out/phase23_spy_relative_prereg.md): active return
``α·strat_R − SPY_%`` under the project policy 1R = α equity, base α = 1%, with a
{0.5, 1, 2}% band; the assumption-free read is each window's own ΔSharpe (strat vs SPY).

This script touches NEITHER the engine NOR run_backtest, so the run_id=15 regression gate
stays byte-identical (it only reads WFResult.oos_point.trades).

    .venv/Scripts/python.exe scripts/wf_benchmark_relative.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import numpy as np   # noqa: E402
import yaml          # noqa: E402

from backtest.benchmark_metrics import (  # noqa: E402
    align_strategy_benchmark, information_ratio, month_end_returns, pct_periods_beating,
)
from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.stats_utils import sharpe_ratio   # noqa: E402
from backtest.walk_forward import WalkForwardEngine  # noqa: E402

RISK_BAND = [0.005, 0.010, 0.020]
RISK_BASE = 0.010


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Walk-forward benchmark-relative truth (fixed-config OOS vs SPY)")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (prices/ behavioral/ macro/ earnings_history/)")
    ap.add_argument("--workers", type=int, default=0,
                    help="Unused for fixed-config WF (sequential); kept for CLI symmetry.")
    ap.add_argument("--tickers", nargs="+", default=None,
                    help="Restrict universe (smoke test only — full tier_a by default).")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = args.tickers or [t for t in wl.get("tier_a", wl.get("tickers", []))
                               if isinstance(t, str)]

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
    if uni.spy_df is None:
        print("  ✗ SPY not loaded from snapshot — cannot run the WF-vs-SPY comparison.")
        return

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 5.0,
        "earnings_aware": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    }
    be = exec_cfg.get("breakeven_trigger_r")
    if be:
        base_port["breakeven_trigger_r"] = float(be)        # filters.yaml ships 1.0R
        if exec_cfg.get("breakeven_buffer_atr"):
            base_port["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    wfe = WalkForwardEngine(uni, base_cfg=base_cfg, base_port_cfg=base_port,
                            is_years=3, oos_years=1, step_months=6, re_tune=False)

    def _progress(msg: str) -> None:
        print(f"  ▸ {msg}", flush=True)

    print("  Running fixed-config walk-forward (3yr IS / 1yr OOS / 6mo step)…", flush=True)
    report = wfe.run(progress=_progress)

    spy_monthly = month_end_returns(uni.spy_df["close"])

    rows = []
    pooled_sR: list[float] = []
    pooled_sP: list[float] = []
    for r in report.results:
        oos_trades = r.oos_point.trades
        if not oos_trades:
            continue
        monthly = build_curve(oos_trades).monthly
        if monthly is None or len(monthly) < 2:
            continue
        periods, sR, sP = align_strategy_benchmark(monthly, spy_monthly)
        if len(periods) < 2:
            continue
        pooled_sR.extend((sR * RISK_BASE).tolist())
        pooled_sP.extend(sP.tolist())
        own_strat = sharpe_ratio(sR.tolist())
        own_spy = sharpe_ratio(sP.tolist())
        ir_band = {a: information_ratio(sR * a, sP) for a in RISK_BAND}
        rows.append(dict(
            w=r.window.index, oos=f"{r.window.oos_start}→{r.window.oos_end}",
            n=len(periods), oos_er=r.oos_er,
            ir_base=ir_band[RISK_BASE], ir_band=ir_band,
            beat=pct_periods_beating(sR * RISK_BASE, sP),
            d_sharpe=own_strat - own_spy,
        ))

    if not rows:
        print("  ✗ no OOS windows produced ≥2 aligned months — cannot evaluate.")
        return

    bar = "  " + "─" * 76
    print("\n" + "=" * 80)
    print("  PHASE 3 — WALK-FORWARD BENCHMARK-RELATIVE TRUTH  (fixed-config OOS vs SPY)")
    print("=" * 80)
    print(f"  {'win':>4} {'OOS window':>23} {'mo':>4} {'OOS E[R]':>9} "
          f"{'exSh@1%':>8} {'%beat':>6} {'ΔSharpe':>8} {'beat?':>6}")
    print(bar)
    for r in rows:
        print(f"  {r['w']:>4} {r['oos']:>23} {r['n']:>4} {r['oos_er']:>+9.3f} "
              f"{r['ir_base']:>+8.2f} {r['beat']*100:>5.0f}% {r['d_sharpe']:>+8.2f} "
              f"{('YES' if r['ir_base'] > 0 else 'no'):>6}")
    print(bar)

    # ── pre-registered verdict (phase23_spy_relative_prereg.md Phase 3) ──
    nW = len(rows)
    pct_excess_pos = {a: sum(1 for r in rows if r["ir_band"][a] > 0) / nW for a in RISK_BAND}
    pooled = {a: information_ratio(np.asarray(pooled_sR) * (a / RISK_BASE),
                                   np.asarray(pooled_sP)) for a in RISK_BAND}
    pct_dsharpe_pos = sum(1 for r in rows if r["d_sharpe"] > 0) / nW

    print(f"  windows: {nW}   pooled OOS months: {len(pooled_sR)}")
    print(f"  ASSUMPTION-FREE co-read: {pct_dsharpe_pos*100:.0f}% of OOS windows beat SPY on own ΔSharpe")
    print()
    print(f"  {'1R=':>6} | {'%win excess>0':>13} | {'pooled exSh':>11} | {'verdict':>9}")
    print(bar)
    for a in RISK_BAND:
        pe, ps = pct_excess_pos[a], pooled[a]
        # PASS = ≥60% windows positive excess AND pooled exSh > 0; FAIL = <50% OR pooled ≤ 0
        if pe < 0.50 or ps <= 0:
            v = "FAIL"
        elif pe >= 0.60 and ps > 0:
            v = "PASS"
        else:
            v = "MARGINAL"
        tag = "  ← base" if a == RISK_BASE else ""
        print(f"  {a*100:>5.1f}% | {pe*100:>12.1f}% | {ps:>+11.2f} | {v:>9}{tag}")
    print(bar)
    base_pe, base_ps = pct_excess_pos[RISK_BASE], pooled[RISK_BASE]
    base_v = ("FAIL" if (base_pe < 0.50 or base_ps <= 0)
              else "PASS" if (base_pe >= 0.60 and base_ps > 0) else "MARGINAL")
    print(f"    >>> PHASE 3 VERDICT (base 1R=1%): {base_v}  "
          f"({base_pe*100:.0f}% windows beat SPY, pooled exSh {base_ps:+.2f}) <<<")
    print("    note: ~overlapping OOS windows → the %% is descriptive; pooled exSh is primary.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
