#!/usr/bin/env python3
"""
Stage 2 — paired A/B for cross-sectional top-K (docs/backtest_out/xsec_topk_prereg.md).

Both legs share ONE universe load, so the data is identical by construction and
the only difference is selection:

  baseline    top_k = None   absolute: every candidate clearing its gates
                             competes for the budget in scan order
  treatment   top_k = K      relative: only the K highest-ranked candidates on a
                             bar may fill

Judged on the pre-registered bar, which was fixed BEFORE any of the Stage-0
diagnostics were seen and is therefore unfittable to them:

  * relative Sharpe >= +18.6%  (the s7-binding DSR requirement)
  * paired monthly-delta bootstrap CI excludes 0 on the positive side
  * TAIL GUARD (overrides Sharpe): total R must not fall, and the top-4% of
    trades must retain >=95% of the baseline's absolute R contribution

The tail guard is a hard kill. The edge is right-tail driven, so a Sharpe gain
bought by trimming the tail is a regression however good the ratio looks.

    python scripts/studies/topk_paired_ab.py --k 5 --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SHARPE_BAR = 0.186      # +18.6% relative — the binding (seed-7) DSR requirement
TAIL_KEEP = 0.95        # top-4% must retain >=95% of baseline absolute R


def _stationary_bootstrap_ci(diff, n_boot, mean_block, seed, alpha=0.05):
    """Percentile CI for the mean of a serially-correlated monthly series.

    Politis-Romano stationary bootstrap: geometric block lengths, wrap-around
    resampling. Monthly R differences are autocorrelated (overlapping holds,
    regime persistence), so an iid bootstrap would understate the interval.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    x = np.asarray(diff, dtype=float)
    T = len(x)
    p = 1.0 / max(mean_block, 1e-9)
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = np.empty(T, dtype=int)
        i = rng.integers(0, T)
        for t in range(T):
            idx[t] = i
            if rng.random() < p:
                i = rng.integers(0, T)
            else:
                i = (i + 1) % T
        means[b] = x[idx].mean()
    return (float(np.percentile(means, 100 * alpha / 2)),
            float(np.percentile(means, 100 * (1 - alpha / 2))),
            float((means <= 0).mean()))


def _tail_r(trades, frac):
    """Absolute R contributed by the top ``frac`` of trades by r_multiple."""
    rs = sorted((float(t.r_multiple) for t in trades), reverse=True)
    n = max(1, int(len(rs) * frac))
    return float(sum(rs[:n])), n


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import numpy as np
    import pandas as pd
    import yaml

    from backtest.equity_curve import build_curve
    from backtest.loader import load_universe
    from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
    from backtest.stats import compute_stats
    from core.filter_engine import FilterEngine
    from core.indicators.rp_rank import build_rp_rank_matrix

    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, required=True,
                    help="Frozen K from Stage 1a. Passed explicitly so the value "
                         "used is visible in the command and the log.")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--mean-block", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rank-min-names", type=int, default=50)
    args = ap.parse_args()

    snap = _ROOT / args.snapshot
    base_cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", []) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices", earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro", behavioral_dir=snap / "behavioral",
        start_date=date.fromisoformat(args.start),
    )
    print(f"  {uni.summary()}", flush=True)
    ranks = build_rp_rank_matrix({tk: p.df for tk, p in uni.prepped.items()})
    print(f"  rank matrix {ranks.shape[0]}x{ranks.shape[1]}", flush=True)

    exec_cfg = base_cfg.get("execution", {})
    base_kwargs = dict(
        max_open_risk=5.0, earnings_aware=True,
        entry_slippage_pct=float(exec_cfg.get("entry_slippage_pct", 0.002)),
        exit_slippage_pct=float(exec_cfg.get("exit_slippage_pct", 0.0) or 0.0),
        commission_r=float(exec_cfg.get("commission_r", 0.005)),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    if exec_cfg.get("breakeven_trigger_r"):
        base_kwargs["breakeven_trigger_r"] = float(exec_cfg["breakeven_trigger_r"])
        if exec_cfg.get("breakeven_buffer_atr"):
            base_kwargs["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    def run(label, **extra):
        cfg = PortfolioConfig(**dict(base_kwargs, **extra))
        t0 = time.time()
        r = PortfolioBacktester(FilterEngine.from_dict(base_cfg), cfg).run_prepped(
            uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
            macro_series=uni.macro_series, behavioral_data=uni.behavioral_data,
            spy_df=uni.spy_df, settings=settings)
        print(f"  {label:<10} {len(r.trades):>5} trades  "
              f"rank_dropped {r.rank_dropped:>5}  ({time.time() - t0:.0f}s)", flush=True)
        return r

    print("\n  Running both legs on one universe load…", flush=True)
    base = run("baseline")
    treat = run("top-K", top_k=args.k, rank_matrix=ranks,
                rank_min_names=args.rank_min_names)

    sb, st = compute_stats(base.trades), compute_stats(treat.trades)
    cb, ct = build_curve(base.trades), build_curve(treat.trades)

    print("\n" + "=" * 78)
    print(f"  STAGE 2 — TOP-K PAIRED A/B   (K={args.k}, one universe load)")
    print("=" * 78)
    print(f"  {'leg':<12}{'trades':>7}{'WR':>7}{'E[R]':>9}{'totalR':>10}"
          f"{'Sharpe':>8}{'Sortino':>9}{'maxDD':>8}")
    for lbl, s, c in (("baseline", sb, cb), (f"top-{args.k}", st, ct)):
        print(f"  {lbl:<12}{s.trades_count:>7}{s.win_rate:>7.1%}{s.expectancy_r:>+9.3f}"
              f"{c.total_r:>+10.2f}{c.sharpe:>8.2f}{c.sortino:>9.2f}{c.max_dd:>8.2f}")

    # ── paired monthly delta ─────────────────────────────────────────────────
    mb, mt = cb.monthly, ct.monthly
    months = sorted(set(mb.index) | set(mt.index))
    vb = np.array([float(mb.get(m, 0.0)) for m in months])
    vt = np.array([float(mt.get(m, 0.0)) for m in months])
    diff = vt - vb
    lo, hi, p_le0 = _stationary_bootstrap_ci(
        diff, args.bootstrap, args.mean_block, args.seed)

    rel_sharpe = (ct.sharpe - cb.sharpe) / abs(cb.sharpe) if cb.sharpe else float("nan")

    print("\n  PAIRED MONTHLY DELTA  (treatment − baseline, same months)")
    print(f"    months            : {len(months)}")
    print(f"    mean delta        : {diff.mean():+.4f} R/month  "
          f"(total {diff.sum():+.2f} R)")
    print(f"    95% CI (stationary bootstrap, B={args.bootstrap}, "
          f"block={args.mean_block:g}mo) : [{lo:+.4f}, {hi:+.4f}]")
    print(f"    P(delta <= 0)     : {p_le0:.4f}")

    # ── tail guard ───────────────────────────────────────────────────────────
    print("\n  TAIL CONTRIBUTION  (absolute R from the top slice of trades)")
    print(f"    {'slice':<10}{'baseline':>22}{'top-' + str(args.k):>22}{'kept':>9}")
    # The guard has TWO independent limbs and they fail for different reasons —
    # kept separate so the verdict names the actual cause. A run can preserve the
    # tail perfectly and still lose total R (the treatment simply traded worse in
    # the body), which is not "trimming the tail" and must not be reported as it.
    tail_kept_ok = True
    for frac in (0.01, 0.02, 0.04, 0.10):
        rb, nb = _tail_r(base.trades, frac)
        rt, nt = _tail_r(treat.trades, frac)
        kept = rt / rb if rb else float("nan")
        if frac == 0.04 and np.isfinite(kept) and kept < TAIL_KEEP:
            tail_kept_ok = False
        print(f"    top {frac:>5.0%}{rb:>+16.2f} ({nb:>4}t){rt:>+16.2f} ({nt:>4}t)"
              f"{kept:>9.1%}" + ("   <-- guard" if frac == 0.04 else ""))
    total_ok = ct.total_r >= cb.total_r
    tail_ok = tail_kept_ok and total_ok

    # ── pre-registered verdict ───────────────────────────────────────────────
    c_sharpe = np.isfinite(rel_sharpe) and rel_sharpe >= SHARPE_BAR
    c_ci = lo > 0.0

    print("\n" + "=" * 78)
    print("  PRE-REGISTERED STAGE-2 VERDICT")
    print("  " + "-" * 74)
    print(f"  [{'PASS' if c_sharpe else 'FAIL'}]  relative Sharpe >= +18.6%      "
          f"→ {cb.sharpe:.3f} → {ct.sharpe:.3f} = {rel_sharpe:+.1%}")
    print(f"  [{'PASS' if c_ci else 'FAIL'}]  paired CI excludes 0 (positive) "
          f"→ [{lo:+.4f}, {hi:+.4f}]")
    print(f"  [{'PASS' if total_ok else 'FAIL'}]  guard a: total R not reduced   "
          f"→ {cb.total_r:+.2f} → {ct.total_r:+.2f}")
    print(f"  [{'PASS' if tail_kept_ok else 'FAIL'}]  guard b: top-4% R >= 95% kept  "
          f"→ {(_tail_r(treat.trades, 0.04)[0] / _tail_r(base.trades, 0.04)[0]):.1%}")
    print("  " + "-" * 74)
    if c_sharpe and c_ci and tail_ok:
        print("  >>> PROCEED to Stage 3 (full gate, both seeds, --spy-relative)")
    else:
        print("  >>> REFUTED at Stage 2 — did not clear the pre-registered bar.")
        if not tail_kept_ok:
            print("      Cause: TAIL TRIMMED. A Sharpe gain bought by cutting the right")
            print("      tail is a regression — the edge IS the tail.")
        elif not total_ok:
            print("      Cause: total R fell while the tail was PRESERVED — the treatment")
            print("      lost in the body of the distribution, not by cutting winners.")
        else:
            print("      Cause: the effect is not large or not distinguishable enough.")
        print("      The bar was fixed before any Stage-0 diagnostic was seen and is")
        print("      not revisited now.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
