#!/usr/bin/env python3
"""
Multiple-testing correction (Phase D) — does the headline edge survive the search?

Runs the OFAT parameter sweep (the same grid as `run_backtest.py --sweep`), then
applies two data-snooping corrections to the set of configs tried:

  • Deflated Sharpe Ratio (DSR, Bailey & López de Prado 2014) — the probability
    the headline's true Sharpe beats the expected MAXIMUM Sharpe attainable by
    chance across the N configs searched.
  • White's Reality Check (RC, White 2000; stationary bootstrap) — H0: the best
    of the N configs has no edge over cash. A small p-value ⇒ real outperformance.

The base config reproduces the current headline (213-name tier_a, scoring OFF,
25-bar if_not_profit, open-risk budget 5.0, slippage 0.002). Each config's
per-month R series comes from `build_curve` (effective-R), so the numbers tie out
to the published Sharpe 0.66 / E[R] +0.075.

    python scripts/multiple_testing.py                       # full grid (~40 min)
    python scripts/multiple_testing.py --quick --workers 14  # reduced-grid smoke
    python scripts/multiple_testing.py --workers 14 --bootstrap 10000 --seed 7

Exploratory — does NOT journal. Read-only on the DB. Needs the price cache
(data/prices).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


def _build_grid(quick: bool) -> list:
    """Filter-param grid; --quick reduces each spec to first/mid/last."""
    from backtest.sweep import PARAM_GRID, ParamSpec
    if not quick:
        return list(PARAM_GRID)
    reduced = []
    for spec in PARAM_GRID:
        vals = list(spec.values)
        if len(vals) >= 3:
            mid = len(vals) // 2
            vals = [vals[0], vals[mid], vals[-1]]
        reduced.append(ParamSpec(dotted=spec.dotted, values=tuple(vals),
                                 label=spec.label, group=spec.group, fmt=spec.fmt))
    return reduced


def _quick_portfolio_grid() -> list:
    from backtest.sweep import PORTFOLIO_GRID, ParamSpec
    reduced = []
    for spec in PORTFOLIO_GRID:
        vals = list(spec.values)
        if len(vals) > 2:
            vals = [vals[0], vals[-1]]
        reduced.append(ParamSpec(dotted=spec.dotted, values=tuple(vals),
                                 label=spec.label, group=spec.group, fmt=spec.fmt))
    return reduced


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import numpy as np
    import yaml

    from backtest.loader import load_universe
    from backtest.sweep import SweepEngine, PORTFOLIO_GRID
    from backtest.equity_curve import build_curve
    from backtest.multiple_testing import (
        align_monthly_matrix,
        deflated_sharpe_ratio,
        whites_reality_check,
    )

    ap = argparse.ArgumentParser(
        description="Phase D — deflated Sharpe + White's reality check over the sweep grid")
    ap.add_argument("--quick", action="store_true",
                    help="Reduced grid (first/mid/last per param) — fast smoke run")
    ap.add_argument("--workers", type=int, default=None,
                    help="Parallel sweep workers (default: SweepEngine auto)")
    ap.add_argument("--bootstrap", type=int, default=5000,
                    help="White reality-check bootstrap resamples (default 5000)")
    ap.add_argument("--mean-block", type=float, default=6.0,
                    help="Stationary-bootstrap mean block length in months (default 6)")
    ap.add_argument("--seed", type=int, default=42, help="Bootstrap RNG seed")
    ap.add_argument("--max-hold-days", type=int, default=25)
    ap.add_argument("--max-hold-mode", default="if_not_profit")
    ap.add_argument("--max-open-risk", type=float, default=5.0)
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                    help="First entry date (inclusive). Default: earliest bar.")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                    help="Last entry date (inclusive). Default: latest bar.")
    ap.add_argument("--tickers", nargs="+", metavar="TICKER", default=None,
                    help="Restrict to these tickers (default: full tier_a). "
                         "Useful for a fast end-to-end smoke test.")
    args = ap.parse_args()

    from datetime import date
    start_date = date.fromisoformat(args.start) if args.start else None
    end_date = date.fromisoformat(args.end) if args.end else None

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]
    if args.tickers:
        tickers = [t for t in args.tickers]

    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
                        earnings_aware=True, start_date=start_date, end_date=end_date)
    print(f"  {uni.summary()}", flush=True)

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": float(args.max_open_risk),
        "earnings_aware": True,
        "entry_slippage_pct": float(exec_cfg.get("entry_slippage_pct", 0.002)),
        "commission_r": float(exec_cfg.get("commission_r", 0.005)),
        "close_open_at_eod": True,
        "max_hold_days": int(args.max_hold_days),
        "max_hold_mode": str(args.max_hold_mode).replace("-", "_"),
    }

    engine = SweepEngine(uni, base_cfg=base_cfg, base_port_cfg=base_port,
                         n_workers=args.workers, use_scoring=False)

    grid = _build_grid(args.quick)
    port_grid = PORTFOLIO_GRID if not args.quick else _quick_portfolio_grid()
    n_jobs = sum(len(s.values) - 1 for s in grid + port_grid)
    print(f"\n  Sweep: {len(grid) + len(port_grid)} params, ~{n_jobs} configs "
          f"(+ baseline)\n", flush=True)

    def _progress(msg: str) -> None:
        print(f"  ▸ {msg}", flush=True)

    report = engine.run_ofat(grid=grid, port_grid=port_grid, progress=_progress)

    # ── per-config monthly-R series (effective-R, via build_curve) ─────────────
    def _sharpe(vals) -> float:
        arr = np.asarray(vals, dtype=float)
        if len(arr) < 2:
            return float("nan")
        sd = arr.std(ddof=1)
        return float(arr.mean() / sd) if sd > 0 else float("nan")

    kept_series = []      # list of monthly pd.Series (≥2 traded months)
    kept_labels = []
    excluded = 0
    baseline_idx = None

    for pt in report.all_points:
        ec = build_curve(pt.trades)
        if ec.monthly is None or len(ec.monthly) < 2:
            excluded += 1
            continue
        if pt.is_baseline:
            baseline_idx = len(kept_series)
        kept_labels.append("headline" if pt.is_baseline else f"{pt.param_label}={pt.param_value}")
        kept_series.append(ec.monthly)

    n_valid = len(kept_series)
    if baseline_idx is None or n_valid < 2:
        print("\n  ✗ Not enough valid configs to run the correction "
              f"(valid={n_valid}, excluded={excluded}).")
        return

    # Build the zero-filled contiguous monthly matrix ONCE, then derive every
    # config's Sharpe from those same columns. This keeps the DSR inputs and the
    # White-RC statistic on one consistent return convention (a no-trade month =
    # 0 R deployed), instead of mixing a sparse per-config Sharpe with a
    # zero-filled RC mean.
    matrix, months = align_monthly_matrix(kept_series)   # shape (T, n_valid)
    all_sharpes = [_sharpe(matrix[:, j]) for j in range(matrix.shape[1])]

    finite_mask = np.isfinite(all_sharpes)
    n_finite = int(finite_mask.sum())
    if n_finite < 2:
        print("\n  ✗ Too few configs with non-degenerate monthly variance to deflate "
              f"(finite Sharpes={n_finite} of {n_valid}).")
        return

    # ── deflated Sharpe: headline + empirical best ────────────────────────────
    dsr_headline = deflated_sharpe_ratio(matrix[:, baseline_idx], all_sharpes, n_trials=n_finite)

    best_idx = int(np.nanargmax(all_sharpes))
    dsr_best = deflated_sharpe_ratio(matrix[:, best_idx], all_sharpes, n_trials=n_finite)

    # ── White's reality check across all configs ──────────────────────────────
    rc = whites_reality_check(matrix, n_bootstrap=args.bootstrap,
                              mean_block=args.mean_block, seed=args.seed)

    # ── verdict ───────────────────────────────────────────────────────────────
    bl_stats = report.baseline.stats
    sr_m = dsr_headline.sr_hat
    sr_ann = sr_m * (12 ** 0.5)

    def _verdict(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    print("\n" + "=" * 74)
    print("  Phase D — Multiple-Testing Correction")
    print("  " + "-" * 70)
    print(f"  Universe : {uni.n_tradeable} names | scoring OFF | "
          f"{base_port['max_hold_days']}d {base_port['max_hold_mode']} | "
          f"budget {base_port['max_open_risk']:g} | slip {base_port['entry_slippage_pct']:g}")
    print(f"  Trials   : N={n_finite} configs entering deflation "
          f"({n_valid} valid, {excluded} excluded <2 months, {n_valid - n_finite} degenerate) | "
          f"T={dsr_headline.n_periods} months")
    print(f"  Headline : E[R] {bl_stats.expectancy_r:+.3f} | "
          f"Sharpe(monthly) {sr_m:+.3f} (annualised {sr_ann:.2f}) | "
          f"{bl_stats.trades_count} trades")
    print()
    print("  Deflated Sharpe Ratio (Bailey & López de Prado 2014)")
    print(f"    SR0 expected-max hurdle (per-period, N={n_finite}) : {dsr_headline.sr0:.3f}")
    print(f"    PSR(0) headline, un-deflated                      : {dsr_headline.psr_vs_zero:.3f}")
    print(f"    DSR headline  = PSR(SR0)                          : "
          f"{dsr_headline.dsr:.3f}   [{_verdict(dsr_headline.dsr > 0.95)}  (>0.95)]")
    print(f"    DSR best-config ({kept_labels[best_idx][:28]:<28})   : "
          f"{dsr_best.dsr:.3f}")
    print()
    print(f"  White's Reality Check (stationary bootstrap, B={rc.n_bootstrap}, "
          f"block={rc.mean_block:g}mo, benchmark=0)")
    print(f"    Best config : {kept_labels[rc.best_config_idx]}  (V={rc.observed_stat:.3f})")
    print(f"    p-value     : {rc.p_value:.4f}   "
          f"[{_verdict(rc.p_value < 0.05)}  (<0.05)]")
    print("  " + "-" * 70)
    # White's RC is the primary snooping test (does the best of N beat cash?);
    # the headline DSR is the conservative confidence that the SHIPPED config's
    # Sharpe beats the chance-maximum over N trials. Read them together.
    rc_pass = rc.p_value < 0.05
    if rc_pass and dsr_headline.dsr > 0.95:
        verdict = "edge SURVIVES the haircut — best config beats cash (RC) AND headline clears the chance-max (DSR)"
    elif rc_pass:
        verdict = (f"best config's edge SURVIVES White's RC (p<0.05); headline DSR={dsr_headline.dsr:.2f} "
                   f"→ {dsr_headline.dsr:.0%} confidence its Sharpe beats the N-trial chance-max (MARGINAL if <0.95)")
    else:
        verdict = "edge does NOT clearly survive the snooping correction (RC p≥0.05)"
    print(f"  Verdict  : {verdict}")
    print("  Caveat   : OFAT trials are highly correlated (each differs from the")
    print("             headline by one knob), so the effective number of independent")
    print("             trials < N — the DSR hurdle is, if anything, conservative.")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
