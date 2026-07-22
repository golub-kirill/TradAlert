#!/usr/bin/env python3
"""
Stage 1a — fix K for cross-sectional top-K (docs/backtest_out/xsec_topk_prereg.md).

K is the number of names top-K selection keeps per bar. It is set to the
BASELINE's mean open-position count so the treatment deploys the same average
risk, which makes the A/B a pure SELECTION change.

Any other K silently changes average deployed exposure, and exposure in R-space
is Sharpe-neutral by construction — so a K-driven "win" would be an artifact of
leverage, not of choosing better names.

This must run, and K must be recorded, BEFORE any top-K result is observed.

Concurrency is derived from the trade ledger (entry_date..exit_date), not from
new engine instrumentation, so the baseline replay is untouched.

    python scripts/studies/baseline_concurrency.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MIN_VIABLE_K = 3   # below this, top-K is not meaningfully different from baseline


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import numpy as np
    import pandas as pd
    import yaml

    from backtest.loader import load_universe
    from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
    from core.filter_engine import FilterEngine

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--start", default="2000-01-01")
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

    exec_cfg = base_cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=5.0, earnings_aware=True,
        entry_slippage_pct=float(exec_cfg.get("entry_slippage_pct", 0.002)),
        exit_slippage_pct=float(exec_cfg.get("exit_slippage_pct", 0.0) or 0.0),
        commission_r=float(exec_cfg.get("commission_r", 0.005)),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    if exec_cfg.get("breakeven_trigger_r"):
        kwargs["breakeven_trigger_r"] = float(exec_cfg["breakeven_trigger_r"])
        if exec_cfg.get("breakeven_buffer_atr"):
            kwargs["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    print("  Running baseline…", flush=True)
    t0 = time.time()
    res = PortfolioBacktester(FilterEngine.from_dict(base_cfg),
                              PortfolioConfig(**kwargs)).run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series, behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df, settings=settings)
    print(f"  {len(res.trades)} trades in {time.time() - t0:.0f}s", flush=True)

    cal = sorted({ts.date() for p in uni.prepped.values() for ts in p.df.index})
    pos = {d: i for i, d in enumerate(cal)}
    held = Counter()
    for t in res.trades:
        if t.entry_date is None:
            continue
        i = pos.get(t.entry_date)
        j = pos.get(t.exit_date) if t.exit_date else None
        if i is None:
            continue
        if j is None or j < i:
            j = i
        for k in range(i, j + 1):
            held[k] += 1

    counts = np.array([held.get(i, 0) for i in range(len(cal))])
    active = counts[counts > 0]
    mean_active = float(active.mean())
    k = int(round(mean_active))

    print("\n" + "=" * 74)
    print("  Stage 1a — BASELINE CONCURRENCY  (fixes K before any top-K result)")
    print("=" * 74)
    print(f"  trading days               : {len(cal)}")
    print(f"  days holding >=1 position  : {len(active)}  ({len(active) / len(cal):.1%})")
    print(f"  mean concurrency (active)  : {mean_active:.3f}")
    print(f"  mean concurrency (all days): {float(counts.mean()):.3f}")
    print(f"  median / p90 / max         : {np.median(active):.0f} / "
          f"{np.percentile(active, 90):.0f} / {active.max():.0f}")
    print(f"  budget max_open_risk       : {kwargs['max_open_risk']:g}")
    print("\n  concurrency histogram (active days)")
    hist = Counter(active.tolist())
    for n in sorted(hist):
        bar = "#" * max(1, int(60 * hist[n] / len(active)))
        print(f"    {n:>2} open : {hist[n]:>5} days  {hist[n] / len(active):5.1%}  {bar}")

    # ── the pool top_k actually chooses from ─────────────────────────────────
    # Open-position count is NOT the quantity that decides whether top_k binds.
    # top_k filters the candidates QUEUED ON A BAR; if that queue is usually
    # shorter than K, selection never fires and an A/B measures a no-op.
    ch = res.candidate_hist
    tot_bars = sum(ch.values())
    tot_cands = sum(n * c for n, c in ch.items())
    print("\n  candidates queued per bar (the pool top_k selects from)")
    if tot_bars:
        for n in sorted(ch):
            share = ch[n] / tot_bars
            bar = "#" * max(1, int(50 * share))
            print(f"    {n:>2} queued : {ch[n]:>5} bars  {share:5.1%}  {bar}")
        mean_c = tot_cands / tot_bars
        print(f"  bars with >=1 candidate : {tot_bars}")
        print(f"  mean queue length       : {mean_c:.2f}")
        for probe in (2, 3, 4, 5):
            binds = sum(c for n, c in ch.items() if n > probe)
            print(f"  bars where K={probe} would bind : {binds:>5}  "
                  f"({binds / tot_bars:5.1%} of candidate bars)")

    print("\n  " + "-" * 70)
    print(f"  >>> K = {k}   (round of mean active concurrency {mean_active:.3f})")
    if k < MIN_VIABLE_K:
        print(f"  >>> ABANDON: K={k} < {MIN_VIABLE_K}. Selecting the top {k} of a book that")
        print("      already holds about that many is not a selection change. Records as")
        print("      INCONCLUSIVE, not refuted.")
    else:
        print("      Frozen. Record it in the prereg and do not revisit it after seeing")
        print("      any treatment result.")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
