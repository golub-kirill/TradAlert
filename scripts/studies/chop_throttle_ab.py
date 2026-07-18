"""
Paired A/B/C/D on a pinned data snapshot: chop-state de-grossing
(the D-009c throttle study, seeded by the D-010 chronic-loser result).

Four legs share ONE load_universe() call and the full shipped execution config:

    A  baseline          (no de-grossing)
    B  chop throttle     (market-state: entry size ×0.5 while the trailing
                          60 sessions saw ≥4 sign flips of SPY close − MA50 —
                          the regime's own trend vote whipsawing; series
                          shifted one session so every mult uses strictly
                          prior closes)
    C  chronic penalty   (per-ticker: the shipped chronic_loser_penalty scale)
    D  both              (B × C, multiplicative)

BOUNDARIES (what this is NOT — all previously refuted, do not drift):
sizes entries only, never blocks (no veto); reads MARKET state, never own
equity (not the drawdown breaker); exits untouched (not the chop-exit tactic).
Thresholds are fixed a priori (≥4 flips/60d ≈ whipsaw ≥ once per 3 weeks) —
the sensitivity variants are REPORTED, never selected from.

Decision rule → docs/backtest_out/chop_throttle_prereg.md (fixed BEFORE the run).

Usage:
    python scripts/studies/chop_throttle_ab.py
        [--snapshot data/snapshot_2026-06-10] [--start 2000-01-01]

Exploratory harness: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402
from core.ticker_health import TickerHealth  # noqa: E402

_ERAS = ((2000, 2010), (2011, 2017), (2018, 2026))

# Fixed a priori — see the pre-registration. Not tunable after results.
FLIP_WINDOW = 60      # trailing sessions counted
FLIP_THRESHOLD = 4    # ≥ this many sign flips → chop
THROTTLE_MULT = 0.5   # entry size multiplier while chopped


def chop_throttle_series(spy_df: pd.DataFrame, ma_window: int,
                         *, window: int = FLIP_WINDOW,
                         threshold: int = FLIP_THRESHOLD,
                         mult: float = THROTTLE_MULT) -> dict:
    """{date: mult} — throttled while the trailing window whipsawed.

    Sign flips of (close − MA) over the trailing ``window`` sessions, shifted
    one session so the mult applied on a fill date uses strictly prior closes.
    """
    close = spy_df["close"].astype(float)
    ma = close.rolling(ma_window).mean()
    sign = (close > ma).astype(int)
    flips = sign.diff().abs().rolling(window).sum()
    chopped = (flips >= threshold).shift(1).fillna(False)
    return {ts.date(): (mult if flag else 1.0) for ts, flag in chopped.items()}


def _run(uni, base_cfg, settings, *, throttle=None, chronic=False):
    exec_cfg = base_cfg.get("execution", {})
    kwargs = dict(
        max_open_risk=float((settings.get("risk") or {}).get("max_open_risk", 5.0)),
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    be = exec_cfg.get("breakeven_trigger_r")
    if be:
        kwargs["breakeven_trigger_r"] = float(be)
        if exec_cfg.get("breakeven_buffer_atr"):
            kwargs["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])
    if throttle is not None:
        kwargs["size_throttle"] = throttle
    if chronic:
        chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
        kwargs["ticker_health"] = TickerHealth.from_config(
            {**chronic_cfg, "enabled": True})
    pcfg = PortfolioConfig(**kwargs)
    engine = FilterEngine.from_dict(base_cfg)
    bt = PortfolioBacktester(engine, pcfg)
    t0 = time.time()
    result = bt.run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series,
        behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df,
        settings=settings,
    )
    print(f"  leg done in {time.time() - t0:.0f}s "
          f"({len(result.trades)} trades, {len(result.capped_signals)} capped)",
          flush=True)
    return result.trades


def _row(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<18} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  "
            f"{ec.sharpe:>6.2f}  {ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


def _delta_line(label, base, leg):
    ecb, ecl = build_curve(base), build_curve(leg)
    stb, stl = compute_stats(base), compute_stats(leg)
    return (f"  {label:<18} {ecl.total_r - ecb.total_r:>+8.2f}R  "
            f"{ecl.sharpe - ecb.sharpe:>+7.3f} SR  "
            f"{ecl.sortino - ecb.sortino:>+7.3f} So  "
            f"{ecl.max_dd - ecb.max_dd:>+7.2f}R maxDD  "
            f"{stl.expectancy_r - stb.expectancy_r:>+7.4f} E[R]")


def _era_table(legs: dict):
    base = legs["baseline"]
    print("\n  ERA STABILITY — ΔSR vs baseline (a one-era carry fails the rule)")
    hdr = "  " + f"{'era':<12}" + "".join(f"{k:>12}" for k in legs if k != "baseline")
    print(hdr)
    for lo, hi in _ERAS:
        b = [t for t in base if lo <= t.entry_date.year <= hi]
        if not b:
            continue
        cells = []
        for name, trades in legs.items():
            if name == "baseline":
                continue
            l = [t for t in trades if lo <= t.entry_date.year <= hi]
            cells.append(f"{build_curve(l).sharpe - build_curve(b).sharpe:>+12.3f}")
        print(f"  {f'{lo}-{hi}':<12}" + "".join(cells))
        cells_r = []
        for name, trades in legs.items():
            if name == "baseline":
                continue
            l = [t for t in trades if lo <= t.entry_date.year <= hi]
            cells_r.append(f"{build_curve(l).total_r - build_curve(b).total_r:>+12.2f}")
        print(f"  {'  ΔR':<12}" + "".join(cells_r))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--start", default="2000-01-01",
                    help="Reduced window = wiring smoke ONLY; the decision "
                         "reads the full window")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot
    start = date.fromisoformat(args.start)

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", []))
               if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    if start != date(2000, 1, 1):
        print(f"  REDUCED WINDOW from {start} — wiring smoke, NOT the decision run",
              flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral",
        start_date=start,
    )
    print(f"  {uni.summary()}", flush=True)

    ma_fast = int(base_cfg.get("trend", {}).get("ma_fast", 50))
    spy = uni.market_dfs.get("SPY") if uni.market_dfs else None
    if spy is None:
        print("  SPY frame missing — cannot build the chop series"); sys.exit(1)
    throttle = chop_throttle_series(spy, ma_fast)
    active = [d for d, m in throttle.items() if m < 1.0 and d >= start]
    span = [d for d in throttle if d >= start]
    print(f"  Chop series: MA{ma_fast}, {FLIP_THRESHOLD}+ flips/{FLIP_WINDOW}d → "
          f"×{THROTTLE_MULT} · throttled {len(active)}/{len(span)} sessions "
          f"({100.0 * len(active) / max(len(span), 1):.0f}%)", flush=True)

    legs = {}
    legs["baseline"] = _run(uni, base_cfg, settings)
    legs["throttle"] = _run(uni, base_cfg, settings, throttle=throttle)
    legs["chronic"] = _run(uni, base_cfg, settings, chronic=True)
    legs["both"] = _run(uni, base_cfg, settings, throttle=throttle, chronic=True)

    print()
    print("  PAIRED A/B/C/D — one universe load, identical data all legs")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    for name, trades in legs.items():
        print(_row(name, trades))
    print("  " + "─" * 80)
    for name in ("throttle", "chronic", "both"):
        print(_delta_line(f"Δ {name}", legs["baseline"], legs[name]))

    _era_table(legs)

    print("\n  DECISION RULE (pre-registered, docs/backtest_out/chop_throttle_prereg.md):")
    print("    ADOPT a leg iff ΔSharpe ≥ +0.02 AND ΔmaxDD ≤ −1.0R AND era ΔSR ≥ 0")
    print("    in ≥ 2 of 3 AND 2011-2017 ΔR > 0; several pass → highest ΔSharpe,")
    print("    ties → fewest knobs (B or C over D).")
    print("    PROTECTION-ONLY (maxDD ≤ −2R, |ΔSharpe| < 0.02, era-stable) → owner")
    print("    call D-011, not silent adoption. Otherwise CLOSE the de-gross family.\n")


if __name__ == "__main__":
    main()
