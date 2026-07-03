#!/usr/bin/env python3
"""
Does insider buying (Form-4) have standalone predictive power?

Cheap, PRE-REGISTERED predictive gate (criteria in `docs/backtest_out/form4_gate_prereg.md`),
run BEFORE any engine integration — the R1 pattern (`scripts/studies/r1_rank_ic.py`). Reads the per-ticker
Form-4 caches from `scripts/studies/form4_fetch.py` + the pinned price snapshot, builds a point-in-time
monthly panel of trailing-90d insider features and forward-21d returns over the US single-stocks,
and reports the two pre-registered bars + the gate verdict.

Point-in-time: features at month-end T use only filings with `filing_date < T`; the forward return
is realized strictly after T. Directions were declared in the prereg before measurement.

Pure helpers (panel build, rank-IC, features) are import-safe (tests/test_form4_gate.py); main() does I/O.

    .venv/Scripts/python.exe scripts/studies/form4_gate.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

CACHE_DIR = _ROOT / "data" / "behavioral" / "form4"
WINDOW_DAYS = 90
FWD = 21          # primary forward horizon (trading days)
FWD2 = 63         # secondary (reported, not gated)
START = pd.Timestamp("2003-07-01")
END = pd.Timestamp("2026-05-31")


# ── pure helpers ──────────────────────────────────────────────────────────────

def rank_ic(feature: np.ndarray, target: np.ndarray) -> tuple[float, float, int]:
    """Spearman rank-IC, its t-stat, and the pairwise-complete N (R1 convention)."""
    mask = np.isfinite(feature) & np.isfinite(target)
    n = int(mask.sum())
    if n < 3:
        return float("nan"), float("nan"), n
    from scipy.stats import spearmanr
    ic = float(spearmanr(feature[mask], target[mask]).correlation)
    if not np.isfinite(ic):
        return float("nan"), float("nan"), n
    t = ic * np.sqrt((n - 2) / max(1e-12, 1.0 - ic * ic))
    return ic, float(t), n


def composite_score(features: pd.DataFrame) -> pd.Series:
    """Mean of available per-feature percentile ranks (all higher-better; no inversion).
    Rows with no feature value sit at the neutral 0.5 (R1 convention)."""
    ranks = {c: features[c].rank(pct=True) for c in features.columns}
    return pd.DataFrame(ranks).mean(axis=1, skipna=True).fillna(0.5)


def trailing_features(fdates: np.ndarray, codes: np.ndarray, values: np.ndarray,
                      owners: np.ndarray, t: np.ndarray,
                      window_days: int = WINDOW_DAYS) -> dict:
    """Point-in-time trailing-window insider features as of decision day `t`, using only
    rows with ``t - window_days <= filing_date < t`` (strictly before t → no look-ahead).

    `fdates` (datetime64), `codes` ('P'/'S'/…), `values` ($), `owners` (CIK str) are one
    ticker's transactions. Returns the 3 pre-registered features + an `active` flag.
    """
    lo = t - np.timedelta64(window_days, "D")
    m = (fdates >= lo) & (fdates < t)
    if not m.any():
        return dict(net_buy_count_90d=0.0, net_buy_value_90d=0.0,
                    distinct_buyers_90d=0.0, active=False)
    c, v, o = codes[m], values[m], owners[m]
    is_p, is_s = (c == "P"), (c == "S")
    return dict(
        net_buy_count_90d=float(is_p.sum() - is_s.sum()),
        net_buy_value_90d=float(v[is_p].sum() - v[is_s].sum()),
        distinct_buyers_90d=float(len(set(o[is_p].tolist()))),
        active=bool(is_p.any() or is_s.any()),
    )


def month_end_decision_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each calendar month present in a price index, within [START, END]."""
    idx = index[(index >= START) & (index <= END)]
    if len(idx) == 0:
        return pd.DatetimeIndex([])
    s = pd.Series(idx, index=idx)
    return pd.DatetimeIndex(s.groupby(idx.to_period("M")).last().to_numpy())


def build_ticker_panel(ticker: str, close: pd.Series, f4: pd.DataFrame) -> pd.DataFrame:
    """One ticker's monthly panel: features as of each month-end T + forward FWD/FWD2 returns."""
    close = close.sort_index()
    pos = {d: i for i, d in enumerate(close.index)}
    decide = month_end_decision_days(close.index)
    if f4 is not None and len(f4):
        fdates = pd.to_datetime(f4["filing_date"]).to_numpy()
        codes = f4["code"].to_numpy().astype(str)
        values = f4["value"].to_numpy(dtype=float)
        owners = f4["owner_cik"].astype(str).to_numpy()
    else:
        fdates = np.array([], dtype="datetime64[ns]")
        codes = np.array([], dtype=str)
        values = np.array([], dtype=float)
        owners = np.array([], dtype=str)
    rows = []
    cvals = close.to_numpy()
    for t in decide:
        i = pos[t]
        if i + FWD >= len(cvals):
            continue
        feat = trailing_features(fdates, codes, values, owners, np.datetime64(t))
        fwd = cvals[i + FWD] / cvals[i] - 1.0
        fwd2 = (cvals[i + FWD2] / cvals[i] - 1.0) if i + FWD2 < len(cvals) else np.nan
        rows.append(dict(ticker=ticker, date=t, year=t.year, fwd=fwd, fwd2=fwd2, **feat))
    return pd.DataFrame(rows)


def evaluate_gate(panel: pd.DataFrame) -> dict:
    """Compute both pre-registered bars + the verdict from the pooled panel. Pure."""
    feat_cols = ["net_buy_count_90d", "net_buy_value_90d", "distinct_buyers_90d"]
    panel = panel.copy()
    panel["composite"] = composite_score(panel[feat_cols])

    # IC bar — over ACTIVE cells only (≥1 P/S in trailing window), vs forward-21d
    act = panel[panel["active"]].copy()
    ic, t, n = rank_ic(act["composite"].to_numpy(), act["fwd"].to_numpy())
    ics_y = []
    for _, g in act.groupby("year"):
        yic, _, yn = rank_ic(g["composite"].to_numpy(), g["fwd"].to_numpy())
        if np.isfinite(yic):
            ics_y.append(yic)
    pos_years = sum(1 for v in ics_y if v > 0)
    frac_years = pos_years / len(ics_y) if ics_y else float("nan")
    pass_ic = bool(np.isfinite(ic) and ic >= 0.03 and t >= 2.0
                   and np.isfinite(frac_years) and frac_years >= 0.60)

    # Economic bar — monthly net-buy bucket fwd minus universe-mean fwd (+ long-short)
    spreads, ls_spreads = [], []
    for _, g in panel.groupby("date"):
        uni = g["fwd"].mean()
        buy = g.loc[g["net_buy_count_90d"] >= 1, "fwd"]
        sell = g.loc[g["net_buy_count_90d"] <= -1, "fwd"]
        if len(buy) >= 3 and np.isfinite(uni):
            spreads.append(buy.mean() - uni)
        if len(buy) >= 3 and len(sell) >= 3:
            ls_spreads.append(buy.mean() - sell.mean())
    spreads = np.array(spreads, dtype=float)
    ls = np.array(ls_spreads, dtype=float)
    mean_spread = float(spreads.mean()) if len(spreads) else float("nan")
    t_spread = (float(mean_spread / (spreads.std(ddof=1) / np.sqrt(len(spreads))))
                if len(spreads) > 1 and spreads.std(ddof=1) > 0 else float("nan"))
    mean_ls = float(ls.mean()) if len(ls) else float("nan")
    pass_econ = bool(np.isfinite(mean_spread) and mean_spread > 0.0020
                     and np.isfinite(t_spread) and t_spread >= 2.0
                     and np.isfinite(mean_ls) and mean_ls > 0)

    return dict(
        ic=ic, ic_t=t, ic_n=n, frac_years=frac_years, pos_years=pos_years,
        n_years=len(ics_y), pass_ic=pass_ic,
        mean_spread=mean_spread, t_spread=t_spread, n_months=len(spreads),
        mean_ls=mean_ls, n_ls_months=len(ls), pass_econ=pass_econ,
        verdict=("PROCEED" if (pass_ic and pass_econ) else "CLOSED"),
        n_active=int(panel["active"].sum()), n_panel=len(panel),
    )


# ── I/O + report ─────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Form-4 insider predictive gate (F4-1)")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = (Path(args.snapshot) if Path(args.snapshot).is_absolute()
            else _ROOT / args.snapshot)
    prices = snap / "prices"

    caches = sorted(CACHE_DIR.glob("*.parquet"))
    caches = [c for c in caches if not c.stem.startswith("_")]
    if not caches:
        raise SystemExit(f"no Form-4 caches in {CACHE_DIR} — run scripts/studies/form4_fetch.py first")

    panels = []
    n_with_data = 0
    for c in caches:
        ticker = c.stem
        pf = prices / f"{ticker}.parquet"
        if not pf.exists():
            continue
        close = pd.read_parquet(pf, columns=["close"])["close"]
        close.index = pd.to_datetime(close.index)
        f4 = pd.read_parquet(c)
        if len(f4):
            n_with_data += 1
        p = build_ticker_panel(ticker, close, f4)
        if len(p):
            panels.append(p)
    panel = pd.concat(panels, ignore_index=True)
    print(f"  panel: {len(panel)} stock-months over {panel['ticker'].nunique()} tickers "
          f"({n_with_data} with ≥1 Form-4 txn); active cells: {int(panel['active'].sum())}",
          flush=True)
    print(f"  decision dates {panel['date'].min():%Y-%m} → {panel['date'].max():%Y-%m}")

    g = evaluate_gate(panel)

    print("\n" + "=" * 78)
    print("  F4-1 — Form-4 insider-buying standalone predictive power")
    print("=" * 78)
    print("\n  IC bar (composite rank-IC vs fwd-21d, ACTIVE cells; pre-reg ≥0.03, t≥2, ≥60% +yrs)")
    print("  " + "─" * 74)
    print(f"    rank-IC = {g['ic']:+.4f}   t = {g['ic_t']:+.2f}   N = {g['ic_n']}")
    print(f"    positive years = {g['pos_years']}/{g['n_years']} ({g['frac_years']:.0%})")
    print(f"    → IC bar: {'PASS' if g['pass_ic'] else 'FAIL'}")
    print("\n  Economic bar (net-buy bucket fwd-21d − universe; pre-reg >+0.20%/mo, t≥2, L/S>0)")
    print("  " + "─" * 74)
    print(f"    mean monthly spread = {g['mean_spread']*100:+.3f}%/mo   t = {g['t_spread']:+.2f}   "
          f"months = {g['n_months']}")
    print(f"    net-buy − net-sell long-short = {g['mean_ls']*100:+.3f}%/mo   "
          f"months = {g['n_ls_months']}")
    print(f"    → economic bar: {'PASS' if g['pass_econ'] else 'FAIL'}")
    print("\n  " + "─" * 74)
    print(f"    >>> F4-1 GATE: {g['verdict']} <<<   "
          f"({'PROCEED — build the signal + re-run the program' if g['verdict'] == 'PROCEED' else 'CLOSED — negative; reconsider short-interest / beta-sleeve'})")
    print("  Coverage ceiling: US single-stocks only (~41% of tier_a; ETFs + .TO excluded).")
    print("=" * 78 + "\n")
    sys.exit(0 if g["verdict"] == "PROCEED" else 3)


if __name__ == "__main__":
    main()
