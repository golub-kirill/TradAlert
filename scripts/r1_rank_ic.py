"""
R1 milestone 1 — is ANY entry-time ranking real at the budget-fill seam?

Reads the ledgers dumped by `oracle_ceiling.py --dump-dir` (candidates.parquet =
every unconstrained-twin trade, the counterfactual outcome ledger;
binddays.parquet = run A's per-day fills + matched capped candidates), builds
the four OWNER-FROZEN features offline (no engine changes), measures the rank-IC
of each against realized eff_r, then replays the bind-day fill with
feature-rank instead of foresight.

Features (pre-registered 2026-06-10; composite directions declared here, BEFORE
measurement — higher composite = ranked first):
  stop_distance_atr   |entry − initial_stop| / ATR14 at entry. Direction:
                      SMALLER is better (a wide gap over the signal bar means a
                      chased entry with degraded R geometry).
  trailing_ticker_exp mean eff_r of the same ticker's candidates EXITED in the
                      prior 90 calendar days (strictly causal: exit < entry).
                      Direction: higher is better.
  regime_bucket       causal expanding mean eff_r of prior SAME-REGIME
                      candidates (exit < entry, min 30 priors else NaN — no
                      invented neutral values). Direction: higher is better.
  rs_vs_spy_20d       20d ticker return minus 20d SPY return, both ending the
                      trading day BEFORE entry (entries are T+1 open fills; the
                      decision uses data through T). Direction: higher is better.

Composite = mean of available feature percentile ranks (stop_distance_atr
inverted), skipping NaNs — a row missing every feature gets the neutral 0.5.

Gate (pre-registered): composite rank-IC ≥ 0.05 with t ≥ 2 AND feature-ranked
replay fill-gain ≥ +0.5R/yr. Below either bar → R1 CLOSED as a negative result.

Pure-math helpers take DataFrames and are import-safe
(tests/test_r1_rank_ic.py); main() does the I/O.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


# ── pure helpers ──────────────────────────────────────────────────────────────

def causal_group_mean(df: pd.DataFrame, group_col: str,
                      window_days: int | None = None,
                      min_prior: int = 1) -> np.ndarray:
    """Per row: mean eff_r of same-group rows whose exit_date is STRICTLY before
    the row's entry_date (outcome already known at decision time), optionally
    restricted to exits within the trailing `window_days`. NaN below
    `min_prior` observations.

    Needs columns: entry_date, exit_date (datetime64), eff_r, `group_col`.
    """
    out = np.full(len(df), np.nan)
    entries = df["entry_date"].to_numpy()
    for _, idx in df.groupby(group_col, sort=False).indices.items():
        sub = df.iloc[idx]
        order = np.argsort(sub["exit_date"].to_numpy())
        exits = sub["exit_date"].to_numpy()[order]
        cumsum = np.concatenate([[0.0], np.cumsum(sub["eff_r"].to_numpy()[order])])
        for i in idx:
            e = entries[i]
            hi = np.searchsorted(exits, e, side="left")
            lo = 0
            if window_days is not None:
                lo = np.searchsorted(exits, e - np.timedelta64(window_days, "D"),
                                     side="left")
            n = hi - lo
            if n >= min_prior:
                out[i] = (cumsum[hi] - cumsum[lo]) / n
    return out


def atr14_before(df: pd.DataFrame, when) -> float:
    """Wilder ATR(14) over the bars STRICTLY before `when` (the last closed bar
    at a T+1-open fill). NaN with fewer than 15 bars. Columns: high, low, close;
    DatetimeIndex ascending."""
    bars = df.loc[: when - pd.Timedelta(days=1)]
    if len(bars) < 15:
        return float("nan")
    prev_close = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"],
                    (bars["high"] - prev_close).abs(),
                    (bars["low"] - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])


def return_20d_before(close: pd.Series, when) -> float:
    """close[T]/close[T-20] − 1 where T is the last bar strictly before `when`."""
    sub = close.loc[: when - pd.Timedelta(days=1)]
    if len(sub) < 21:
        return float("nan")
    return float(sub.iloc[-1] / sub.iloc[-21] - 1.0)


def rank_ic(feature: np.ndarray, target: np.ndarray) -> tuple[float, float, int]:
    """Spearman rank-IC, its t-stat, and the pairwise-complete N."""
    mask = np.isfinite(feature) & np.isfinite(target)
    n = int(mask.sum())
    if n < 3:
        return float("nan"), float("nan"), n
    from scipy.stats import spearmanr
    ic = float(spearmanr(feature[mask], target[mask]).correlation)
    t = ic * np.sqrt((n - 2) / max(1e-12, 1.0 - ic * ic))
    return ic, float(t), n


def composite_score(features: pd.DataFrame, invert: tuple[str, ...]) -> pd.Series:
    """Mean of available per-feature percentile ranks; `invert` columns flipped.
    Rows with no feature at all sit at the neutral 0.5."""
    ranks = {}
    for col in features.columns:
        r = features[col].rank(pct=True)
        ranks[col] = (1.0 - r) if col in invert else r
    comp = pd.DataFrame(ranks).mean(axis=1, skipna=True)
    return comp.fillna(0.5)


def replay_fill(bind: pd.DataFrame, score: pd.Series) -> dict:
    """Re-run the bind-day fill picking top-K by `score` (aligned to bind.index)
    instead of insertion order. K = the day's actual fill count; the same
    count-K capacity proxy the oracle uses. Returns totals plus the oracle's,
    for capture context."""
    bind = bind.assign(_score=score)
    actual = feature = oracle = 0.0
    n_days = 0
    for _, day in bind.groupby("date", sort=False):
        k = int((day["source"] == "fill").sum())
        if k == 0 or len(day) <= k:
            continue              # no contest on this day
        n_days += 1
        actual += day.loc[day["source"] == "fill", "eff_r"].sum()
        feature += day.nlargest(k, "_score")["eff_r"].sum()
        oracle += day.nlargest(k, "eff_r")["eff_r"].sum()
    return {"actual": actual, "feature": feature, "oracle": oracle,
            "days": n_days}


def bind_scores(scores: pd.Series, cand: pd.DataFrame,
                bind: pd.DataFrame) -> tuple[pd.Series, int]:
    """Align a per-candidate score (indexed like `cand`) onto bind-day rows via
    (ticker, date) ↔ (ticker, entry_date). Unmatched rows (path divergence)
    get the neutral 0.5; returns (scores, n_unmatched)."""
    key = pd.MultiIndex.from_frame(cand[["ticker", "entry_date"]])
    s = pd.Series(scores.to_numpy(), index=key)
    s = s[~s.index.duplicated()]
    bk = pd.MultiIndex.from_frame(bind[["ticker", "date"]])
    out = pd.Series(s.reindex(bk).to_numpy(), index=bind.index)
    n_miss = int(out.isna().sum())
    return out.fillna(0.5), n_miss


# ── I/O + measurement ─────────────────────────────────────────────────────────

def _load_close(prices_dir: Path, ticker: str) -> pd.Series | None:
    p = prices_dir / f"{ticker}.parquet"
    if not p.exists():
        return None
    return pd.read_parquet(p, columns=["close"])["close"]


def main() -> None:
    ap = argparse.ArgumentParser(description="R1 rank-IC + feature-ranked replay")
    ap.add_argument("--dump-dir", default="docs/backtest_out/r1")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()

    dump = Path(args.dump_dir) if Path(args.dump_dir).is_absolute() \
        else _ROOT / args.dump_dir
    prices_dir = (Path(args.snapshot) if Path(args.snapshot).is_absolute()
                  else _ROOT / args.snapshot) / "prices"

    cand = pd.read_parquet(dump / "candidates.parquet")
    bind = pd.read_parquet(dump / "binddays.parquet")
    for col in ("entry_date", "exit_date"):
        cand[col] = pd.to_datetime(cand[col])
    bind["date"] = pd.to_datetime(bind["date"])
    print(f"  candidates: {len(cand)} rows · bind-day rows: {len(bind)} "
          f"({bind['date'].nunique()} days)")

    # ── features ──────────────────────────────────────────────────────────────
    spy = _load_close(prices_dir, "SPY")
    if spy is None:
        raise SystemExit(f"SPY.parquet missing under {prices_dir}")

    stop_dist = np.full(len(cand), np.nan)
    rs20 = np.full(len(cand), np.nan)
    for ticker, idx in cand.groupby("ticker", sort=False).indices.items():
        pf = prices_dir / f"{ticker}.parquet"
        if not pf.exists():
            continue
        px = pd.read_parquet(pf)
        for i in idx:
            when = cand["entry_date"].iat[i]
            atr = atr14_before(px, when)
            if np.isfinite(atr) and atr > 0:
                stop_dist[i] = abs(cand["entry_price"].iat[i]
                                   - cand["initial_stop"].iat[i]) / atr
            r_t = return_20d_before(px["close"], when)
            r_s = return_20d_before(spy, when)
            if np.isfinite(r_t) and np.isfinite(r_s):
                rs20[i] = r_t - r_s

    feats = pd.DataFrame({
        "stop_distance_atr": stop_dist,
        "trailing_ticker_exp": causal_group_mean(cand, "ticker",
                                                 window_days=90, min_prior=1),
        "regime_bucket": causal_group_mean(cand, "market_regime",
                                           min_prior=30),
        "rs_vs_spy_20d": rs20,
    }, index=cand.index)
    comp = composite_score(feats, invert=("stop_distance_atr",))

    # ── rank-IC vs realized eff_r ─────────────────────────────────────────────
    target = cand["eff_r"].to_numpy()
    print("\n  Rank-IC vs realized eff_r (pooled, pairwise-complete):")
    print(f"  {'feature':<22} {'IC':>8} {'t':>7} {'N':>6}  coverage")
    for col in feats.columns:
        ic, t, n = rank_ic(feats[col].to_numpy(), target)
        print(f"  {col:<22} {ic:+8.4f} {t:+7.2f} {n:>6}  "
              f"{feats[col].notna().mean():.0%}")
    ic_c, t_c, n_c = rank_ic(comp.to_numpy(), target)
    print(f"  {'COMPOSITE':<22} {ic_c:+8.4f} {t_c:+7.2f} {n_c:>6}")

    by_year = cand["entry_date"].dt.year
    ics = []
    for y, idx in cand.groupby(by_year, sort=True).indices.items():
        ic, _, n = rank_ic(comp.to_numpy()[idx], target[idx])
        if np.isfinite(ic):
            ics.append(ic)
    pos_years = sum(1 for v in ics if v > 0)
    print(f"  Composite IC by year: mean {np.mean(ics):+.4f}, "
          f"positive {pos_years}/{len(ics)} years")

    # ── feature-ranked bind-day replay ───────────────────────────────────────
    bind_score, n_miss = bind_scores(comp, cand, bind)
    res = replay_fill(bind, bind_score)
    bind_span_y = max(1.0, (bind["date"].max() - bind["date"].min()).days / 365.25)
    gain = res["feature"] - res["actual"]
    ceiling = res["oracle"] - res["actual"]
    print(f"\n  Bind-day replay ({res['days']} contested days, "
          f"{n_miss} unmatched rows scored neutral):")
    print(f"    actual (insertion order): {res['actual']:+8.1f}R")
    print(f"    feature-ranked          : {res['feature']:+8.1f}R   "
          f"gain {gain:+.1f}R ({gain / bind_span_y:+.2f}R/yr)")
    print(f"    oracle (foresight)      : {res['oracle']:+8.1f}R   "
          f"ceiling {ceiling:+.1f}R ({ceiling / bind_span_y:+.2f}R/yr)")
    if ceiling > 0:
        print(f"    capture of ceiling      : {100 * gain / ceiling:.1f}%")

    # ── post-hoc diagnostics (NOT gate inputs) ───────────────────────────────
    # Single-feature replays in the pre-declared directions: tells us whether a
    # future, separately pre-registered single-feature hypothesis is worth a
    # NEW test. Reported after the fact; the gate below ignores these.
    print("\n  Post-hoc single-feature replay (diagnostic only, NOT a gate input):")
    for col in feats.columns:
        r = feats[col].rank(pct=True)
        if col == "stop_distance_atr":
            r = 1.0 - r
        sc, _ = bind_scores(r.fillna(0.5), cand, bind)
        g = replay_fill(bind, sc)
        print(f"    {col:<22} gain "
              f"{(g['feature'] - g['actual']) / bind_span_y:+6.2f}R/yr")

    # ── pre-registered gate ──────────────────────────────────────────────────
    pass_ic = bool(np.isfinite(ic_c) and ic_c >= 0.05 and t_c >= 2.0)
    pass_gain = bool(gain / bind_span_y >= 0.5)
    print("\n  GATE (pre-registered): composite IC ≥ 0.05 (t ≥ 2) AND "
          "replay gain ≥ +0.5R/yr")
    print(f"    IC bar  : {'PASS' if pass_ic else 'FAIL'} "
          f"(IC {ic_c:+.4f}, t {t_c:+.2f})")
    print(f"    gain bar: {'PASS' if pass_gain else 'FAIL'} "
          f"({gain / bind_span_y:+.2f}R/yr)")
    print(f"    → R1 milestone 1: "
          f"{'PROCEED to milestone 2' if (pass_ic and pass_gain) else 'CLOSED — negative result, no live hook'}")
    sys.exit(0 if (pass_ic and pass_gain) else 3)


if __name__ == "__main__":
    main()
