#!/usr/bin/env python3
"""
Stage 0 factor validation for cross-sectional top-K (docs/backtest_out/xsec_topk_prereg.md).

Two tests. The SECOND is primary, and it is the cheap kill gate for the whole
lever — no engine changes, no top-K implementation.

  0a  universe-level IC   Spearman(RP rank, forward 21-bar return) across the
                          cross-section on each month-end, plus the quintile
                          forward-return table. The standard factor check.

  0b  conditional-on-signal IC   among trades the BASELINE ACTUALLY TOOK, does
                          entry-time RP rank predict realised R? Top-K selects
                          among signals that already fired, not among all names,
                          so 0b is the test that decides whether top-K can help.
                          IF 0b IS NULL, TOP-K CANNOT HELP REGARDLESS OF 0a.

Pre-registered kill criteria (all must hold to proceed to Stage 1):
  * 0b mean IC > 0 with t > 2
  * 0b IC positive in a majority of calendar years
  * 0a top quintile forward return > bottom quintile

Exploratory — does NOT journal. Read-only on the DB.

    python scripts/studies/rp_factor_ic.py --snapshot data/snapshot_2026-06-10
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

FWD_BARS = 21          # ≈ the 25-bar max_hold horizon the strategy actually trades
MIN_XS = 20            # names needed before a cross-section is worth ranking
MIN_MONTH_TRADES = 5   # trades needed before a month yields an IC


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import numpy as np
    import pandas as pd
    import yaml
    from scipy import stats

    from backtest.loader import load_universe
    from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
    from core.filter_engine import FilterEngine
    from core.indicators.rp_rank import build_rp_rank_matrix

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
    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices", earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro", behavioral_dir=snap / "behavioral",
        start_date=date.fromisoformat(args.start),
    )
    print(f"  {uni.summary()}", flush=True)

    closes = {tk: p.df["close"] for tk, p in uni.prepped.items()}
    print("  Building RP rank matrix…", flush=True)
    t0 = time.time()
    ranks = build_rp_rank_matrix({tk: p.df for tk, p in uni.prepped.items()})
    print(f"  rank matrix {ranks.shape[0]}x{ranks.shape[1]} in {time.time() - t0:.1f}s",
          flush=True)

    # ── 0a: universe-level IC ────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("  0a — UNIVERSE-LEVEL IC   Spearman(RP rank, forward 21-bar return)")
    print("=" * 74)

    close_mat = pd.DataFrame(closes).reindex(ranks.index)
    fwd = close_mat.shift(-FWD_BARS) / close_mat - 1.0

    month_ends = ranks.index.to_series().groupby(
        [ranks.index.year, ranks.index.month]).last()
    ics_a, buckets = [], {q: [] for q in range(1, 6)}
    for ts in month_ends:
        r, f = ranks.loc[ts], fwd.loc[ts]
        ok = r.notna() & f.notna()
        if int(ok.sum()) < MIN_XS:
            continue
        rv, fv = r[ok], f[ok]
        ics_a.append(stats.spearmanr(rv, fv).statistic)
        # quintiles by rank; qcut on ranks handles ties across a growing universe
        try:
            q = pd.qcut(rv, 5, labels=range(1, 6), duplicates="drop")
        except ValueError:
            continue
        for lab in q.cat.categories:
            buckets[int(lab)].append(float(fv[q == lab].mean()))

    ics_a = np.asarray([x for x in ics_a if np.isfinite(x)])
    ic_a_mean = float(ics_a.mean())
    ic_a_t = float(ic_a_mean / (ics_a.std(ddof=1) / np.sqrt(len(ics_a))))
    print(f"  months          : {len(ics_a)}")
    print(f"  mean IC         : {ic_a_mean:+.4f}   t = {ic_a_t:+.2f}   "
          f"IR = {ic_a_mean / ics_a.std(ddof=1):+.3f}")
    print(f"  IC > 0 share    : {float((ics_a > 0).mean()):.1%}")
    print(f"\n  {'quintile':<12}{'mean fwd 21-bar return':>24}")
    qmeans = {}
    for q in range(1, 6):
        qmeans[q] = float(np.mean(buckets[q])) if buckets[q] else float("nan")
        tag = "  (worst RP)" if q == 1 else ("  (best RP)" if q == 5 else "")
        print(f"  Q{q:<11}{qmeans[q]:>+23.4%}{tag}")
    spread_a = qmeans[5] - qmeans[1]
    print(f"  Q5 − Q1         : {spread_a:+.4%}")

    # ── baseline trades ──────────────────────────────────────────────────────
    print("\n  Running baseline backtest for the conditional test…", flush=True)
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
    t0 = time.time()
    res = PortfolioBacktester(FilterEngine.from_dict(base_cfg),
                              PortfolioConfig(**kwargs)).run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series, behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df, settings=settings)
    print(f"  {len(res.trades)} trades in {time.time() - t0:.0f}s", flush=True)

    # ── 0b: conditional-on-signal IC ─────────────────────────────────────────
    print("\n" + "=" * 74)
    print("  0b — CONDITIONAL-ON-SIGNAL IC   (PRIMARY)")
    print("     among trades the baseline TOOK: does entry RP rank predict realised R?")
    print("=" * 74)

    idx = ranks.index
    rows = []
    for t in res.trades:
        if t.entry_date is None or t.ticker not in ranks.columns:
            continue
        # Rank as of the SIGNAL bar: the last row strictly BEFORE the entry fill
        # (entries fill at the next bar's open), mirroring the engine's own
        # look-ahead contract.
        pos = idx.searchsorted(pd.Timestamp(t.entry_date)) - 1
        if pos < 0:
            continue
        rk = ranks.iloc[pos].get(t.ticker, float("nan"))
        if not np.isfinite(rk):
            continue
        rows.append((t.entry_date, float(rk), float(t.r_multiple)))

    df = pd.DataFrame(rows, columns=["entry_date", "rp", "r"])
    print(f"  trades with a rank : {len(df)} of {len(res.trades)}")
    if len(df) < 100:
        print("  ✗ too few ranked trades to judge — VOID")
        sys.exit(1)

    pooled = stats.spearmanr(df["rp"], df["r"])
    print(f"  pooled Spearman    : {pooled.statistic:+.4f}  (p = {pooled.pvalue:.4f})")
    print("     ^ trades overlap in time, so this p-value is optimistic; the")
    print("       monthly series below is the one the criterion reads.")

    df["month"] = pd.to_datetime(df["entry_date"]).dt.to_period("M")
    monthly = []
    for _, g in df.groupby("month"):
        if len(g) < MIN_MONTH_TRADES or g["rp"].nunique() < 2:
            continue
        ic = stats.spearmanr(g["rp"], g["r"]).statistic
        if np.isfinite(ic):
            monthly.append(ic)
    monthly = np.asarray(monthly)
    ic_b_mean = float(monthly.mean())
    ic_b_t = float(ic_b_mean / (monthly.std(ddof=1) / np.sqrt(len(monthly))))
    print(f"\n  monthly IC series  : {len(monthly)} months")
    print(f"  mean IC            : {ic_b_mean:+.4f}   t = {ic_b_t:+.2f}")
    print(f"  IC > 0 share       : {float((monthly > 0).mean()):.1%}")

    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    yearly = {}
    for yr, g in df.groupby("year"):
        if len(g) < 20 or g["rp"].nunique() < 2:
            continue
        ic = stats.spearmanr(g["rp"], g["r"]).statistic
        if np.isfinite(ic):
            yearly[int(yr)] = float(ic)
    pos_years = sum(1 for v in yearly.values() if v > 0)
    print(f"  years with IC > 0  : {pos_years}/{len(yearly)}")
    print("   " + "  ".join(f"{y}:{v:+.2f}" for y, v in sorted(yearly.items())))

    print(f"\n  {'RP quintile at entry':<24}{'trades':>8}{'mean R':>10}{'total R':>10}")
    qb = pd.qcut(df["rp"], 5, labels=range(1, 6), duplicates="drop")
    qspread = {}
    for lab in sorted(qb.cat.categories):
        g = df[qb == lab]
        qspread[int(lab)] = float(g["r"].mean())
        tag = "  (worst RP)" if lab == 1 else ("  (best RP)" if lab == 5 else "")
        print(f"  Q{int(lab):<23}{len(g):>8}{g['r'].mean():>+10.4f}"
              f"{g['r'].sum():>+10.2f}{tag}")

    # ── post-verdict diagnostics (exploratory, NOT part of the criteria) ─────
    # The criteria above are fixed. These only inform whether a DIFFERENT factor
    # formulation is worth one of the three budgeted pre-registrations — they
    # cannot rescue this one, and any hypothesis they suggest must be tested on
    # data that did not generate it.
    print("\n" + "-" * 74)
    print("  diagnostics (exploratory — cannot change the verdict above)")
    print("  " + "-" * 70)

    top_q = int(max(qspread, key=qspread.get))
    gq = df[qb == top_q].sort_values("r", ascending=False)
    rest = df[qb != top_q]["r"]
    print(f"  best quintile is Q{top_q}: {len(gq)} trades, mean R {gq['r'].mean():+.4f}, "
          f"total {gq['r'].sum():+.2f}")
    print(f"  rest of the book : {len(rest)} trades, mean R {rest.mean():+.4f}")
    print("\n  is that quintile a distribution shift, or a few outliers?")
    for drop in (0, 1, 3, 5, 10):
        kept = gq.iloc[drop:]
        print(f"    drop top {drop:>2} trades → {len(kept)} left, "
              f"mean R {kept['r'].mean():+.4f}, total {kept['r'].sum():+8.2f}"
              + ("   ← vs rest %+.4f" % rest.mean() if drop == 0 else ""))
    wr_q = df.groupby(qb, observed=True)["r"].apply(lambda s: float((s > 0).mean()))
    print("\n  win rate by quintile (bigger winners, or more of them?)")
    print("   " + "  ".join(f"Q{int(k)}:{v:.1%}" for k, v in wr_q.items()))
    print("\n  best-quintile mean R by era")
    era = pd.to_datetime(gq["entry_date"]).dt.year // 5 * 5
    for e, g in gq.groupby(era):
        print(f"    {int(e)}-{int(e) + 4}: {len(g):>4} trades  mean R {g['r'].mean():+.4f}")

    # ── pre-registered verdict ───────────────────────────────────────────────
    c1 = ic_b_mean > 0 and ic_b_t > 2.0
    c2 = len(yearly) > 0 and pos_years > len(yearly) / 2
    c3 = np.isfinite(spread_a) and spread_a > 0

    print("\n" + "=" * 74)
    print("  PRE-REGISTERED STAGE-0 VERDICT")
    print("  " + "-" * 70)
    print(f"  [{'PASS' if c1 else 'FAIL'}]  0b mean IC > 0 with t > 2      "
          f"→ IC {ic_b_mean:+.4f}, t {ic_b_t:+.2f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}]  0b IC positive in most years   "
          f"→ {pos_years}/{len(yearly)}")
    print(f"  [{'PASS' if c3 else 'FAIL'}]  0a Q5 > Q1 forward return      "
          f"→ {spread_a:+.4%}")
    print("  " + "-" * 70)
    if c1 and c2 and c3:
        print("  >>> PROCEED to Stage 1 (fix K, re-establish the SPY-relative baseline)")
    else:
        print("  >>> REFUTED at Stage 0 — RP-as-specified does not predict outcomes")
        print("      among the signals top-K would choose between. Per the prereg this")
        print("      closes THIS FORMULATION, not the cross-sectional line; a different")
        print("      factor needs a fresh pre-registration (budget: 3 formulations).")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
