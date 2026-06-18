#!/usr/bin/env python3
"""
Benchmark-relative truth — one leg of the honest-validation program.

Is the run_id=15 edge *alpha*, or disguised SPY *beta*? Runs the headline
breakeven-1.0R leg ONCE on the pinned snapshot (byte-identical to the breakeven
leg of ``scripts/paired_ab.py`` == ``run_id=15``), takes its monthly R series, and
compares it to passive buy-and-hold SPY monthly returns from the SAME snapshot,
over Full / 10y / 5y / 3y / 1y windows.

UNITS (see ``backtest/benchmark_metrics.py`` + ``docs/backtest_out/validation_prereg.md`` §P1-M):
the strategy is in R, SPY in %. Every *excess* metric (IR / excess-Sharpe / %-beating /
alpha / beta) is computed on SAME-UNIT series under the project fixed-risk policy
**1R = 1% equity**, and reported with a {0.5, 1, 2}% IR sensitivity band. The
assumption-free read is the standalone-Sharpe delta (strategy own vs SPY own), and the
doc-literal **raw** ``sharpe(strat_R − SPY_%)`` is printed as a degeneracy control.

Exploratory harness: no journal, no HTML, no CSV. Read-only on the snapshot.

    .venv/Scripts/python.exe scripts/benchmark_relative.py --snapshot data/snapshot_2026-06-10
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
import pandas as pd  # noqa: E402
import yaml          # noqa: E402

from backtest.benchmark_metrics import (  # noqa: E402
    align_strategy_benchmark, alpha_beta, information_ratio, pct_periods_beating,
)
from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.stats import compute_stats        # noqa: E402
from backtest.stats_utils import sharpe_ratio   # noqa: E402

# Reuse the EXACT run_id=15 leg execution → byte-identical to the paired_ab breakeven leg.
from paired_ab import _run  # noqa: E402

WINDOWS = [("Full", None), ("10y", 120), ("5y", 60), ("3y", 36), ("1y", 12)]
RISK_BAND = [0.005, 0.010, 0.020]   # 1R ∈ {0.5%, 1%, 2%} equity — IR sensitivity band
RISK_BASE = 0.010                   # pre-registered base assumption: 1R = 1% equity


def _spy_monthly_returns(spy_df: pd.DataFrame) -> pd.Series:
    """Monthly % returns of buy-and-hold SPY (month-end close → pct_change), same
    convention as ``scripts/benchmark_spy.py``. Indexed by month-end Timestamp."""
    close = spy_df["close"].dropna()
    monthly_close = close.resample("ME").last().dropna()
    return monthly_close.pct_change().dropna()


def _window_mask(periods, months):
    """Boolean mask for the trailing ``months`` calendar months (anchored at the last
    aligned period). ``months=None`` → the full series."""
    if months is None:
        return np.ones(len(periods), dtype=bool)
    start = periods[-1] - (months - 1)  # inclusive trailing window
    return np.asarray(periods >= start)


def verdict_at(rows: list[dict], by: dict, k: float) -> dict:
    """Evaluate the pre-registered benchmark-relative PASS/MARGINAL/FAIL at risk-per-trade ``k``
    (the 1R↔equity factor). Pure function of the per-window metric ``rows`` so it is
    import-safe and unit-testable without the heavy backtest.

    Criteria (validation_prereg.md): PASS = IR(full) ≥ 0.30 AND IR > 0 in ≥3/N windows
    AND %-beat(full) > 50%;  FAIL = IR(full) ≤ 0 OR IR ≤ 0 in BOTH 3y and 1y (FAIL takes
    precedence);  else MARGINAL. IR here is the active-return excess-Sharpe at scaling ``k``.
    """
    irs = {r["label"]: r["ir_band"][k] for r in rows}
    full_ir = irs.get("Full", float("nan"))
    full_beat = by["Full"]["beat_band"][k] if "Full" in by else float("nan")
    pos = sum(1 for v in irs.values() if v > 0)
    r3 = irs.get("3y", float("nan"))
    r1 = irs.get("1y", float("nan"))
    fail = (full_ir <= 0) or (r3 <= 0 and r1 <= 0)
    passing = (full_ir >= 0.30) and (pos >= 3) and (full_beat > 0.50)
    v = "FAIL" if fail else ("PASS" if passing else "MARGINAL")
    return dict(verdict=v, full_ir=full_ir, pos=pos, full_beat=full_beat, fail=fail)


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Benchmark-relative truth (strategy vs SPY)")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (prices/ behavioral/ macro/ earnings_history/)")
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
    if uni.spy_df is None:
        print("  ✗ SPY not loaded from snapshot — cannot run the benchmark-relative comparison.")
        return

    # ── run_id=15 leg (breakeven 1.0R) — byte-identical to the paired_ab breakeven leg ──
    trades = _run(uni, base_cfg, settings, breakeven_trigger_r=1.0)
    st = compute_stats(trades)
    ec = build_curve(trades)
    print(f"  run_id=15 leg: {st.trades_count} trades · {ec.total_r:+.2f}R · "
          f"Sharpe {ec.sharpe:.2f} · maxDD {ec.max_dd:.2f}  "
          f"(expect 1622 / +120.42 / 0.60 / 30.71)", flush=True)

    spy_monthly = _spy_monthly_returns(uni.spy_df)              # month-end % returns
    periods, strat_R, spy_pct = align_strategy_benchmark(ec.monthly, spy_monthly)
    if len(periods) < 2:
        print("  ✗ fewer than 2 aligned months — cannot compute benchmark metrics.")
        return
    print(f"  aligned months: {len(periods)}  ({periods[0]} → {periods[-1]})",
          flush=True)

    # ── per-window metrics ────────────────────────────────────────────────────────
    rows = []
    for label, months in WINDOWS:
        m = _window_mask(periods, months)
        sR, sP = strat_R[m], spy_pct[m]
        if len(sR) < 2:
            continue
        sPct = sR * RISK_BASE                       # strategy in % under 1R = 1% equity
        own_strat = sharpe_ratio(sR.tolist())       # scale-invariant (R or % → same)
        own_spy = sharpe_ratio(sP.tolist())
        ir_base = information_ratio(sPct, sP)        # == excess-Sharpe at 1R = 1%
        beat = pct_periods_beating(sPct, sP)
        alpha_m, beta = alpha_beta(sPct, sP)
        raw_excess = information_ratio(sR, sP)       # doc-literal degenerate control
        ir_band = {r: information_ratio(sR * r, sP) for r in RISK_BAND}
        beat_band = {r: pct_periods_beating(sR * r, sP) for r in RISK_BAND}
        rows.append(dict(
            label=label, n=len(sR), strat_R=float(sR.sum()), spy_pct=float(sP.mean()),
            own_strat=own_strat, own_spy=own_spy, d_sharpe=own_strat - own_spy,
            ir_base=ir_base, beat=beat, alpha_ann=alpha_m * 12.0, beta=beta,
            raw_excess=raw_excess, ir_band=ir_band, beat_band=beat_band,
        ))

    by = {r["label"]: r for r in rows}

    def _f(x, p=2):
        return "   nan" if x != x else f"{x:>+6.{p}f}"

    bar = "  " + "─" * 78
    print("\n" + "=" * 80)
    print("  PHASE 1 — BENCHMARK-RELATIVE TRUTH  (run_id=15 vs buy-and-hold SPY, pinned snapshot)")
    print("=" * 80)

    # A — assumption-free standalone Sharpe (PRIMARY interpretive read; no unit assumption)
    print("\n  A) Standalone Sharpe — ASSUMPTION-FREE (each series' own; the honest risk-adj read)")
    print(bar)
    print(f"  {'window':>6} {'months':>7} {'strat Sharpe':>13} {'SPY Sharpe':>11} "
          f"{'ΔSharpe':>9} {'beats SPY?':>11}")
    print(bar)
    for r in rows:
        print(f"  {r['label']:>6} {r['n']:>7} {_f(r['own_strat']):>13} "
              f"{_f(r['own_spy']):>11} {_f(r['d_sharpe']):>9} "
              f"{('YES' if r['d_sharpe'] > 0 else 'no'):>11}")
    print("     note: SPY Sharpe here is over the STRATEGY-ACTIVE months only (not full-SPY")
    print("     history), so it is not directly comparable to scripts/benchmark_spy.py figures;")
    print("     the within-tool ΔSharpe (identical months both legs) is the intended read.")

    # B — active-return metrics under the pre-registered 1R = 1% equity assumption
    print("\n  B) Active-return metrics — ASSUMPTION-DEPENDENT (1R = 1% equity; pre-registered base)")
    print(bar)
    print(f"  {'window':>6} {'IR/exSh':>8} {'%-beat':>7} {'alpha%/yr':>10} "
          f"{'beta':>6} {'raw exSh':>9}  (raw = doc-literal R−% control)")
    print(bar)
    for r in rows:
        print(f"  {r['label']:>6} {_f(r['ir_base']):>8} {r['beat']*100:>6.1f}% "
              f"{_f(r['alpha_ann']):>10} {_f(r['beta']):>6} {_f(r['raw_excess']):>9}")

    # C — sensitivity of IR *and* %-beating to the risk-per-trade assumption
    print("\n  C) Sensitivity to 1R↔equity assumption  (IR and %-beating across the band)")
    print(bar)
    print(f"  {'window':>6} | {'IR@0.5%':>8} {'IR@1.0%':>8} {'IR@2.0%':>8} "
          f"| {'beat@0.5':>8} {'beat@1.0':>8} {'beat@2.0':>8}")
    print(bar)
    for r in rows:
        ib, bb = r["ir_band"], r["beat_band"]
        print(f"  {r['label']:>6} | {_f(ib[0.005]):>8} {_f(ib[0.010]):>8} {_f(ib[0.020]):>8} "
              f"| {bb[0.005]*100:>7.1f}% {bb[0.010]*100:>7.1f}% {bb[0.020]*100:>7.1f}%")

    # ── pre-registered verdict, evaluated ACROSS the band (§P1-M) ──────────────────
    verdicts = {k: verdict_at(rows, by, k) for k in RISK_BAND}
    base = verdicts[RISK_BASE]
    band_stable = len({verdicts[k]["verdict"] for k in RISK_BAND}) == 1
    nW = len(rows)

    print("\n" + "=" * 80)
    print("  PRE-REGISTERED VERDICT — evaluated ACROSS the 1R↔equity band, not one column (§P1-M)")
    print("  (criteria from validation_prereg.md: PASS = IR(full)≥0.30 AND exSh>0 in ≥3/5 AND")
    print("   %-beat(full)>50%;  FAIL = IR(full)≤0 OR exSh≤0 in BOTH 3y and 1y;  else MARGINAL)")
    print(bar)
    print(f"  {'1R=':>6} | {'IR(full)':>9} {'≥0.30?':>7} | {'exSh>0':>8} | {'%beat(f)':>9} | {'VERDICT':>9}")
    print(bar)
    for k in RISK_BAND:
        d = verdicts[k]
        tag = "  ← pre-registered base" if k == RISK_BASE else ""
        print(f"  {k*100:>5.1f}% | {d['full_ir']:>+9.3f} {('yes' if d['full_ir'] >= 0.30 else 'no'):>7} "
              f"| {d['pos']:>4}/{nW} | {d['full_beat']*100:>7.1f}% | {d['verdict']:>9}{tag}")
    print(bar)
    print(f"    >>> PHASE 1 VERDICT (pre-registered base, 1R = 1% equity): {base['verdict']} <<<")
    if band_stable:
        print(f"    >>> Verdict STABLE across {{0.5, 1, 2}}% band — not driven by the leverage choice <<<")
    else:
        print(f"    >>> Verdict is ASSUMPTION-DEPENDENT: it FLIPS across the band → the 'beats SPY'")
        print(f"        answer is a FUNCTION OF ASSUMED LEVERAGE, not a property of the signal (the")
        print(f"        decisive finding §P1-M anticipated). Read the assumption-free ΔSharpe below. <<<")

    # ── assumption-free co-read (cannot be moved by leverage) ─────────────────────
    full = by.get("Full")
    print("\n  ASSUMPTION-FREE CO-READ — standalone ΔSharpe (cannot be moved by the 1R↔equity choice)")
    print(bar)
    if full is not None:
        print(f"    ΔSharpe(full) = {full['d_sharpe']:+.2f}   "
              f"(strat own {full['own_strat']:.2f} vs SPY own {full['own_spy']:.2f})   "
              f"→ strat {'BEATS' if full['d_sharpe'] > 0 else 'does NOT beat'} SPY on risk-adj")
        recent = [lab for lab in ("10y", "5y", "3y", "1y") if lab in by]
        if recent:
            cells = "  ".join(f"Δ{lab} {by[lab]['d_sharpe']:+.2f}" for lab in recent)
            print(f"    recent windows: {cells}")
        else:
            print("    recent windows: insufficient months on this snapshot")
    else:
        print("    Full window unavailable — cannot compute standalone ΔSharpe.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
