#!/usr/bin/env python3
"""
Bind-frequency diagnostic for two suspected-inert knobs (triage Note 2a/2b).

Both `signals.momentum.short.rsi_min` (the held-long momentum-FADE exit RSI floor)
and `behavioral.breadth_divergence_penalty` swept with ~no effect. This one-off
diagnostic measures how often each actually *binds*, so we can prune the sweep row,
widen its range, or fix wiring — rather than guess.

  1. Momentum-fade RSI floor — over the backtest universe, count bars where the
     fade exit is *eligible* (MACD histogram crosses down through zero AND the
     magnitude gate passes), then how often the RSI band — specifically the lower
     bound `rsi_min` — is the reason the exit is withheld. Mirrors
     `filter_engine._momentum_fade_exit`.

  2. Breadth divergence — frequency of the `breadth_divergence` flag over the full
     breadth history, using the production predicate `core.behavioral._classify_breadth`.
     Also checks the backtest loader's behavioral keys: the classifier reads
     `breadth`/`sector_rotation`, but `loader.load_universe` keys parquet by stem
     (`sp500_breadth`/`sector_ratios`), so breadth is silently MISSING in backtests.

    python scripts/instrument_binds.py

Read-only; no DB. Loads the price cache + data/behavioral/ parquet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── pure tally helpers (unit-tested; no I/O) ────────────────────────────────

def fade_floor_stats(df, rsi_min: float, rsi_max: float, min_hist_delta_atr: float) -> dict:
    """Tally momentum-fade-exit eligibility and RSI-band outcomes over one df.

    Mirrors `filter_engine._momentum_fade_exit`: a fade is *eligible* when the MACD
    histogram crosses down through zero (`prev>0>cur`) AND the drop clears the
    magnitude gate (`cur-prev <= -min_hist_delta_atr*atr`). The RSI band is then
    the only remaining gate. Among eligible bars, classify by the RSI outcome:

        fire    : rsi_min <= rsi <= rsi_max   (exit fires)
        floor   : rsi < rsi_min               (withheld by the lower bound)
        ceiling : rsi > rsi_max               (withheld by the upper bound)

    Needs columns macd_hist, rsi, atr. NaN rows simply don't count.
    """
    import numpy as np
    mh = df["macd_hist"].to_numpy(dtype=float)
    rsi = df["rsi"].to_numpy(dtype=float)
    atr = df["atr"].to_numpy(dtype=float)
    if len(mh) < 2:
        return {"eligible": 0, "fire": 0, "floor": 0, "ceiling": 0}
    prev_mh, cur_mh = mh[:-1], mh[1:]
    cur_rsi, cur_atr = rsi[1:], atr[1:]
    cross_down = (prev_mh > 0) & (cur_mh < 0)
    magnitude = (cur_mh - prev_mh) <= -(min_hist_delta_atr * cur_atr)
    eligible = cross_down & magnitude
    floor = eligible & (cur_rsi < rsi_min)
    ceiling = eligible & (cur_rsi > rsi_max)
    fire = eligible & (cur_rsi >= rsi_min) & (cur_rsi <= rsi_max)
    return {"eligible": int(eligible.sum()), "fire": int(fire.sum()),
            "floor": int(floor.sum()), "ceiling": int(ceiling.sum())}


def merge_fade_stats(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in ("eligible", "fire", "floor", "ceiling")}


def breadth_divergence_frequency(breadth_df, spy_df, window: int = 30) -> dict:
    """Walk the breadth history and count days `breadth_divergence` is True, using
    the production predicate `core.behavioral._classify_breadth`.

    Returns eval_days / divergence_days plus first/last divergence date (ISO or None).
    `window` is the trailing slice fed per day — only the last value (breadth),
    last 5 (trend) and last 20 (SPY high) matter, so 30 is ample and keeps each
    call O(window) instead of O(history).
    """
    from core.behavioral import _classify_breadth
    if breadth_df is None or len(breadth_df) == 0:
        return {"eval_days": 0, "divergence_days": 0, "first": None, "last": None}
    eval_days = div_days = 0
    first = last = None
    for ts in breadth_df.index:
        s = None if spy_df is None else spy_df.loc[:ts]
        if s is None or len(s) < 20:
            continue
        _state, diverged = _classify_breadth(breadth_df.loc[:ts].tail(window), s.tail(window))
        eval_days += 1
        if diverged:
            div_days += 1
            first = first or ts
            last = ts
    return {"eval_days": eval_days, "divergence_days": div_days,
            "first": None if first is None else first.date().isoformat(),
            "last": None if last is None else last.date().isoformat()}


def breadth_key_status(behavioral_keys) -> dict:
    """Does the backtest loader's behavioral dict expose the keys the classifier
    reads? The classifier uses `breadth`/`sector_rotation`; the loader keys parquet
    by stem (`sp500_breadth`/`sector_ratios`). Returns the mismatch verdict."""
    keys = set(behavioral_keys or [])
    return {
        "has_breadth_key": "breadth" in keys,
        "has_sector_rotation_key": "sector_rotation" in keys,
        "stem_breadth_present": "sp500_breadth" in keys,
        "stem_sector_present": "sector_ratios" in keys,
    }


# ── report ──────────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import yaml
    from backtest.loader import load_universe

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    fade = cfg["signals"]["momentum"]["short"]
    rsi_min, rsi_max = float(fade["rsi_min"]), float(fade["rsi_max"])
    mhd = float(fade["min_hist_delta_atr"])

    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]

    print(f"\n  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(tickers, earnings_aware=False)

    # 1. momentum-fade RSI floor
    totals = {"eligible": 0, "fire": 0, "floor": 0, "ceiling": 0}
    for prep in uni.prepped.values():
        totals = merge_fade_stats(totals, fade_floor_stats(prep.df, rsi_min, rsi_max, mhd))

    n = totals["eligible"]
    print("\n" + "=" * 74)
    print(f"  Momentum-fade RSI floor   signals.momentum.short  "
          f"(rsi_min={rsi_min:g}, rsi_max={rsi_max:g}, min_hist_delta_atr={mhd:g})")
    print("  " + "-" * 70)
    print(f"  fade-eligible bars (MACD cross-down + magnitude gate): {n}")
    if n:
        print(f"    fire (RSI in band) : {totals['fire']:>6} ({100*totals['fire']/n:5.1f}%)")
        print(f"    floor (rsi<min)    : {totals['floor']:>6} ({100*totals['floor']/n:5.1f}%)  ← the swept knob")
        print(f"    ceiling (rsi>max)  : {totals['ceiling']:>6} ({100*totals['ceiling']/n:5.1f}%)")
        print("    NB unconditional over all bars (not just held-long bars), and a "
              "withheld\n       fade often defers to another exit — so this is an upper "
              "bound on P&L impact.")
        verdict = ("INERT — floor never binds; prune the sweep row or lower the range"
                   if totals["floor"] == 0 else
                   "rarely binds — consider pruning/widening" if totals["floor"] / n < 0.02
                   else "floor is an active predicate — keep (sweep flatness is likely "
                        "exit-substitution, not inertness)")
        print(f"  → {verdict}")
    else:
        print("  → no fade-eligible bars in the cache; nothing to measure")

    # 2. breadth divergence
    bd = uni.behavioral_data or {}
    ks = breadth_key_status(list(bd.keys()))
    breadth_df = bd.get("breadth")
    if breadth_df is None:
        breadth_df = bd.get("sp500_breadth")  # pre-fix fallback if an old cache lingers
    freq = breadth_divergence_frequency(breadth_df, uni.spy_df)

    print("\n  Breadth divergence        behavioral.breadth_divergence_penalty")
    print("  " + "-" * 70)
    if not ks["has_breadth_key"] and ks["stem_breadth_present"]:
        print("  ⚠ KEY MISMATCH: loader exposes 'sp500_breadth' but the classifier reads")
        print("    'breadth' → breadth is MISSING in the backtest/sweep path, so")
        print("    breadth_divergence is structurally False there regardless of the penalty.")
    d, e = freq["divergence_days"], freq["eval_days"]
    if e:
        print(f"  true divergence frequency on raw breadth (correct keys): "
              f"{d}/{e} days ({100*d/e:.2f}%)")
        print(f"    first: {freq['first'] or '—'}   last: {freq['last'] or '—'}")
        verdict = ("INERT even with correct keys — prune the penalty sweep row"
                   if d / e < 0.01 else "fires occasionally — fix the key wiring, then keep")
        print(f"  → {verdict}")
    else:
        print("  → no breadth history to evaluate")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
