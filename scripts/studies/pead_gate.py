#!/usr/bin/env python3
"""
Does post-earnings drift (PEAD) have standalone predictive power on this universe?

Cheap, PRE-REGISTERED predictive gate (criteria in `docs/backtest_out/pead_gate_prereg.md`),
run BEFORE any engine integration — the R1 / Form-4 pattern (`scripts/studies/form4_gate.py`). Reads the
per-ticker earnings caches from `scripts/fetch/pead_fetch.py` + the pinned price snapshot, builds a
point-in-time EVENT panel (one row per earnings announcement) of the announcement abnormal return
(`car_event`) and forward market-adjusted returns, and reports the two pre-registered bars + verdict.

GATED feature = `car_event` (price-based, clean point-in-time). SUE/surprise is REPORTED, not gated
(yfinance EPS estimate is likely not true pre-announcement consensus → look-ahead risk).

Point-in-time: the signal is observed at the close of the reaction day E (the first session that can
price the release: BMO → announcement-date session, AMC/unknown → next session); entry is the open of
E+1; forward returns are realized strictly after entry. Directions declared in the prereg before
measurement.

Pure helpers (rank-IC, reaction alignment, event returns, terciles) are import-safe
(tests/test_pead_gate.py); main() does I/O.

    .venv/Scripts/python.exe scripts/studies/pead_gate.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

CACHE_DIR = _ROOT / "data" / "earnings_history_pead"
FWDS = (5, 21, 63)        # forward horizons (trading days); 21 = primary GATED, 5/63 reported
FWD = 21
START = pd.Timestamp("2004-01-01")
END = pd.Timestamp("2026-06-10")
MIN_EVENTS_PER_TICKER = 8
MIN_EVENTS_PER_MONTH = 6  # need ≥2 per tercile for a within-month long-short


# ── pure helpers ──────────────────────────────────────────────────────────────

def rank_ic(feature: np.ndarray, target: np.ndarray) -> tuple[float, float, int]:
    """Spearman rank-IC, its t-stat, and the pairwise-complete N (Form-4/R1 convention)."""
    feature = np.asarray(feature, dtype=float)
    target = np.asarray(target, dtype=float)
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


def classify_reaction(local_hour: int) -> str:
    """Map an announcement hour (exchange-local) to the reaction session.

    BMO (before ~noon) → the market prices it in the announcement-date session.
    AMC (>=noon) or unknown (-1) → the NEXT session (conservative, no look-ahead).
    """
    if local_hour is None:
        return "AMC"
    h = int(local_hour)
    if 0 <= h < 12:
        return "BMO"
    return "AMC"


def reaction_pos(dates: np.ndarray, ann_date: np.datetime64, reaction: str) -> int | None:
    """Integer position in a sorted price-date array of the reaction day E.

    BMO → first session on/after the announcement date; AMC → first session strictly
    after it. Returns None if no such session exists in the array.
    """
    side = "left" if reaction == "BMO" else "right"  # >= vs >
    i = int(np.searchsorted(dates, ann_date, side=side))
    return i if i < len(dates) else None


def tercile_long_short(car: np.ndarray, fwd: np.ndarray) -> float:
    """Top-tercile minus bottom-tercile mean forward return, sorted by `car`. NaN if too few."""
    car = np.asarray(car, dtype=float)
    fwd = np.asarray(fwd, dtype=float)
    m = np.isfinite(car) & np.isfinite(fwd)
    car, fwd = car[m], fwd[m]
    if len(car) < MIN_EVENTS_PER_MONTH:
        return float("nan")
    q1, q2 = np.quantile(car, [1.0 / 3.0, 2.0 / 3.0])
    top, bot = fwd[car >= q2], fwd[car <= q1]
    if len(top) == 0 or len(bot) == 0:
        return float("nan")
    return float(top.mean() - bot.mean())


def series_t(x: np.ndarray) -> tuple[float, float, int]:
    """Mean, one-sample t-stat (vs 0), and N of a 1-D series (ignoring NaNs)."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else float("nan")), float("nan"), n
    sd = x.std(ddof=1)
    mean = float(x.mean())
    t = float(mean / (sd / np.sqrt(n))) if sd > 0 else float("nan")
    return mean, t, n


# ── panel build (point-in-time) ─────────────────────────────────────────────────

def build_ticker_panel(ticker: str, prices: pd.DataFrame, earn: pd.DataFrame,
                       spy_close: pd.Series, spy_open: pd.Series,
                       spy_ma50: pd.Series) -> pd.DataFrame:
    """One ticker's EVENT panel: `car_event`, fwd5/21/63 (market-adjusted), SUE, momentum,
    eligibility, year/month — one row per earnings announcement with a complete fwd-21 window."""
    prices = prices.sort_index()
    idx = prices.index
    dvals = idx.values  # datetime64[ns], sorted
    close = prices["close"].to_numpy(dtype=float)
    op = (prices["open"].to_numpy(dtype=float) if "open" in prices.columns else close)
    ma50 = prices["close"].rolling(50).mean().to_numpy(dtype=float)
    is_to = ticker.upper().endswith(".TO")

    def spy_ret_close(d_to: pd.Timestamp, d_from: pd.Timestamp) -> float:
        a, b = spy_close.asof(d_from), spy_close.asof(d_to)
        return (b / a - 1.0) if (np.isfinite(a) and np.isfinite(b) and a > 0) else np.nan

    def spy_ret_oc(d_to: pd.Timestamp, d_from_open: pd.Timestamp) -> float:
        a, b = spy_open.asof(d_from_open), spy_close.asof(d_to)
        return (b / a - 1.0) if (np.isfinite(a) and np.isfinite(b) and a > 0) else np.nan

    rows = []
    n = len(idx)
    for r in earn.itertuples(index=False):
        try:
            ann = pd.Timestamp(r.ann_date).normalize()
        except Exception:
            continue
        reaction = classify_reaction(getattr(r, "local_hour", -1))
        iE = reaction_pos(dvals, np.datetime64(ann), reaction)
        if iE is None or iE < 1 or iE + 1 >= n:
            continue
        dE = idx[iE]
        if dE < START or dE > END:
            continue
        # need the primary fwd-21 window to exist (gate requires it)
        if iE + 1 + FWD >= n:
            continue

        d_prev, d_entry = idx[iE - 1], idx[iE + 1]
        # signal: announcement abnormal return on E (market-adjusted close-to-close)
        car = (close[iE] / close[iE - 1] - 1.0) - spy_ret_close(dE, d_prev)

        fwd = {}
        for h in FWDS:
            j = iE + 1 + h
            if j < n and op[iE + 1] > 0:
                stock = close[j] / op[iE + 1] - 1.0
                fwd[h] = stock - spy_ret_oc(idx[j], d_entry)
            else:
                fwd[h] = np.nan

        # pre-announcement 20d momentum (ends at E_prev → excludes the event day)
        mom20 = np.nan
        if iE - 1 - 20 >= 0:
            mom20 = (close[iE - 1] / close[iE - 1 - 20] - 1.0) - spy_ret_close(d_prev, idx[iE - 1 - 20])

        # engine-eligibility proxy: SPY bull (>MA50) AND stock above its own MA50
        spy_c, spy_m = spy_close.asof(dE), spy_ma50.asof(dE)
        eligible = bool(np.isfinite(spy_c) and np.isfinite(spy_m) and spy_c > spy_m
                        and np.isfinite(ma50[iE]) and close[iE] > ma50[iE])

        rows.append(dict(
            ticker=ticker, is_to=is_to, date=dE, year=int(dE.year),
            month=pd.Period(dE, freq="M"),
            car_event=float(car), sue=float(getattr(r, "surprise_pct", np.nan)),
            mom20=float(mom20), eligible=eligible,
            fwd5=fwd[5], fwd21=fwd[21], fwd63=fwd[63],
        ))
    return pd.DataFrame(rows)


# ── gate evaluation ─────────────────────────────────────────────────────────────

def evaluate_gate(panel: pd.DataFrame) -> dict:
    """Both pre-registered bars + verdict from the pooled EVENT panel. Pure.

    IC bar: rank-IC(car_event, fwd21) ≥ 0.03, t ≥ 2, positive in ≥ 60% of years.
    Economic bar: monthly within-month tercile long-short (top−bottom car_event) fwd21 series,
                  mean > +0.20%/mo, t ≥ 2, AND pooled top−bottom tercile spread > 0.
    """
    car = panel["car_event"].to_numpy()
    fwd = panel["fwd21"].to_numpy()

    ic, ict, icn = rank_ic(car, fwd)
    ics_y = []
    for _, g in panel.groupby("year"):
        yic, _, _ = rank_ic(g["car_event"].to_numpy(), g["fwd21"].to_numpy())
        if np.isfinite(yic):
            ics_y.append((int(g["year"].iloc[0]), yic))
    pos_years = sum(1 for _, v in ics_y if v > 0)
    frac_years = pos_years / len(ics_y) if ics_y else float("nan")
    pass_ic = bool(np.isfinite(ic) and ic >= 0.03 and ict >= 2.0
                   and np.isfinite(frac_years) and frac_years >= 0.60)

    # monthly within-month tercile long-short series
    monthly = []
    for _, g in panel.groupby("month"):
        ls = tercile_long_short(g["car_event"].to_numpy(), g["fwd21"].to_numpy())
        if np.isfinite(ls):
            monthly.append(ls)
    mean_ls_m, t_ls_m, n_months = series_t(np.array(monthly, dtype=float))
    pooled_ls = tercile_long_short(car, fwd)
    pass_econ = bool(np.isfinite(mean_ls_m) and mean_ls_m > 0.0020
                     and np.isfinite(t_ls_m) and t_ls_m >= 2.0
                     and np.isfinite(pooled_ls) and pooled_ls > 0)

    return dict(
        ic=ic, ic_t=ict, ic_n=icn, frac_years=frac_years, pos_years=pos_years,
        n_years=len(ics_y), ics_y=ics_y, pass_ic=pass_ic,
        mean_ls_m=mean_ls_m, t_ls_m=t_ls_m, n_months=n_months, pooled_ls=pooled_ls,
        pass_econ=pass_econ, n_panel=len(panel),
        verdict=("PROCEED" if (pass_ic and pass_econ) else "CLOSED"),
    )


def diagnostics(panel: pd.DataFrame) -> dict:
    """Reported-not-gated: SUE, orthogonality, US/.TO splits, per-horizon IC."""
    car, fwd = panel["car_event"].to_numpy(), panel["fwd21"].to_numpy()
    out = {}

    # SUE (reported, NOT gated — estimate point-in-time risk)
    sue_ic, sue_t, sue_n = rank_ic(panel["sue"].to_numpy(), fwd)
    out["sue"] = dict(ic=sue_ic, t=sue_t, n=sue_n,
                      ls=tercile_long_short(panel["sue"].to_numpy(), fwd))

    # orthogonality: corr(car, pre-announcement momentum) + IC inside engine-eligible cells
    m = np.isfinite(car) & np.isfinite(panel["mom20"].to_numpy())
    corr_mom = (float(np.corrcoef(car[m], panel["mom20"].to_numpy()[m])[0, 1])
                if m.sum() > 2 else float("nan"))
    elig = panel[panel["eligible"]]
    eic, et, en = rank_ic(elig["car_event"].to_numpy(), elig["fwd21"].to_numpy())
    out["ortho"] = dict(corr_mom=corr_mom, elig_ic=eic, elig_t=et, elig_n=en,
                        elig_share=float(panel["eligible"].mean()))

    # US vs .TO
    out["splits"] = {}
    for label, sub in (("US", panel[~panel["is_to"]]), (".TO", panel[panel["is_to"]])):
        sic, st, sn = rank_ic(sub["car_event"].to_numpy(), sub["fwd21"].to_numpy())
        out["splits"][label] = dict(ic=sic, t=st, n=sn,
                                    ls=tercile_long_short(sub["car_event"].to_numpy(),
                                                          sub["fwd21"].to_numpy()))
    # per-horizon IC
    out["horizons"] = {}
    for h in FWDS:
        hic, ht, hn = rank_ic(car, panel[f"fwd{h}"].to_numpy())
        out["horizons"][h] = dict(ic=hic, t=ht, n=hn)
    return out


# ── I/O + report ─────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="PEAD price-drift predictive gate (PEAD-1)")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = (Path(args.snapshot) if Path(args.snapshot).is_absolute()
            else _ROOT / args.snapshot)
    prices_dir = snap / "prices"

    spy = pd.read_parquet(prices_dir / "SPY.parquet")
    spy.index = pd.to_datetime(spy.index)
    spy = spy.sort_index()
    spy_close, spy_open = spy["close"], spy["open"]
    spy_ma50 = spy["close"].rolling(50).mean()

    caches = sorted(CACHE_DIR.glob("*.parquet"))
    if not caches:
        raise SystemExit(f"no PEAD earnings caches in {CACHE_DIR} — run scripts/fetch/pead_fetch.py first")

    panels, n_single, n_dropped = [], 0, 0
    for c in caches:
        ticker = c.stem
        if ticker.upper() == "SPY":
            continue
        pf = prices_dir / f"{ticker}.parquet"
        if not pf.exists():
            continue
        earn = pd.read_parquet(c)
        if len(earn) == 0:
            continue  # ETF / no earnings
        n_single += 1
        prices = pd.read_parquet(pf, columns=["open", "close"])
        prices.index = pd.to_datetime(prices.index)
        p = build_ticker_panel(ticker, prices, earn, spy_close, spy_open, spy_ma50)
        if len(p) < MIN_EVENTS_PER_TICKER:
            n_dropped += 1
            continue
        panels.append(p)

    if not panels:
        raise SystemExit("empty PEAD panel — nothing to gate")
    panel = pd.concat(panels, ignore_index=True)

    n_to = int(panel["is_to"].sum())
    print(f"  panel: {len(panel)} earnings events over {panel['ticker'].nunique()} single-stocks "
          f"({n_single} with earnings; {n_dropped} dropped <{MIN_EVENTS_PER_TICKER} events)")
    print(f"  coverage: US {panel['ticker'].nunique() - panel[panel['is_to']]['ticker'].nunique()} "
          f"+ .TO {panel[panel['is_to']]['ticker'].nunique()}  |  events: US {len(panel)-n_to} / .TO {n_to}")
    print(f"  reaction days {panel['date'].min():%Y-%m} → {panel['date'].max():%Y-%m}")

    g = evaluate_gate(panel)
    d = diagnostics(panel)

    print("\n" + "=" * 78)
    print("  PEAD-1 — post-earnings drift standalone predictive power (GATED: car_event)")
    print("=" * 78)
    print("\n  IC bar (rank-IC car_event vs fwd-21d, all events; pre-reg ≥0.03, t≥2, ≥60% +yrs)")
    print("  " + "─" * 74)
    print(f"    rank-IC = {g['ic']:+.4f}   t = {g['ic_t']:+.2f}   N = {g['ic_n']}")
    print(f"    positive years = {g['pos_years']}/{g['n_years']} ({g['frac_years']:.0%})")
    print(f"    → IC bar: {'PASS' if g['pass_ic'] else 'FAIL'}")
    print("\n  Economic bar (monthly tercile L/S car_event, fwd-21d; pre-reg >+0.20%/mo, t≥2, pooled>0)")
    print("  " + "─" * 74)
    print(f"    monthly L/S = {g['mean_ls_m']*100:+.3f}%/mo   t = {g['t_ls_m']:+.2f}   months = {g['n_months']}")
    print(f"    pooled top−bottom tercile = {g['pooled_ls']*100:+.3f}%")
    print(f"    → economic bar: {'PASS' if g['pass_econ'] else 'FAIL'}")
    print("\n  " + "─" * 74)
    print(f"    >>> PEAD-1 GATE: {g['verdict']} <<<   "
          f"({'PROCEED — build the signal + re-run the program' if g['verdict'] == 'PROCEED' else 'CLOSED — negative; reconsider short-interest / beta-sleeve'})")
    print("=" * 78)

    # ── reported-not-gated diagnostics ──
    print("\n  REPORTED (not gated) — context for the verdict")
    print("  " + "─" * 74)
    s = d["sue"]
    print(f"    SUE / surprise(%): rank-IC = {s['ic']:+.4f}  t = {s['t']:+.2f}  N = {s['n']}  "
          f"tercile L/S = {s['ls']*100:+.3f}%   [CAVEAT: yfinance estimate may not be point-in-time]")
    o = d["ortho"]
    print(f"    orthogonality: corr(car_event, pre-ann 20d momentum) = {o['corr_mom']:+.3f}")
    print(f"                   car_event IC INSIDE engine-eligible cells = {o['elig_ic']:+.4f} "
          f"(t {o['elig_t']:+.2f}, N {o['elig_n']}, {o['elig_share']:.0%} of events eligible)")
    for label in ("US", ".TO"):
        sp = d["splits"][label]
        print(f"    split {label:>3}: rank-IC = {sp['ic']:+.4f}  t = {sp['t']:+.2f}  N = {sp['n']}  "
              f"tercile L/S = {sp['ls']*100:+.3f}%")
    hz = "   ".join(f"fwd{h}: IC {d['horizons'][h]['ic']:+.4f} (t {d['horizons'][h]['t']:+.2f})" for h in FWDS)
    print(f"    horizons: {hz}")
    yrs = "  ".join(f"{y}:{v:+.2f}" for y, v in g["ics_y"])
    print(f"    per-year IC: {yrs}")
    print("=" * 78 + "\n")

    sys.exit(0 if g["verdict"] == "PROCEED" else 3)


if __name__ == "__main__":
    main()
