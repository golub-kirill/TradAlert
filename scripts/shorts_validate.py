#!/usr/bin/env python3
"""
Short-side validation (Phase: shorts) — paired, borrow-honest, vs SPY.

Pre-registered: docs/backtest_out/shorts_validation_prereg.md (FROZEN 2026-06-20).
Runs TWO paired legs on ONE snapshot load (identical data, immune to cache jitter):
long-only (baseline) vs ``signals.allow_shorts=true``. Then evaluates the frozen bars:

  Gate-in : ≥30 BEAR-regime shorts fire (else PENDING/underpowered).
  Bar 1   : shorts-ON Sharpe ≥ long-only × 0.98 AND Calmar(ON) ≥ Calmar(OFF), on
            size+borrow-adjusted effective_r (borrow-honest; via validate_shorts).
  Bar 2   : shorts-ON full-sample excess-Sharpe vs SPY ≥ long-only, at 1% base AND
            sign-stable across the {0.5,1,2}% α-band (§P2-M, insurance framing).
  Bar 3   : in ≥2 of the 3 real-BEAR windows (2008/2020/2022): maxDD(ON) ≤ maxDD(OFF)
            AND excess-Sharpe(ON) > excess-Sharpe(OFF) vs SPY.
  Bar 4   : full-sample maxDD(ON) ≤ maxDD(OFF) + 2R (no drawdown regression).

SHIP (allow_shorts:true) only if gate-in + Bars 1–4 all PASS. effective_r carries
size_mult + borrow (equity_curve.build_curve), so both the economic and the SPY-relative
legs are borrow-honest. Exploratory: no journal, no HTML, no CSV (use --save-ledgers to dump).

    .venv/Scripts/python.exe scripts/shorts_validate.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import copy
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
    align_strategy_benchmark, information_ratio, month_end_returns,
)
from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.validate_shorts import (  # noqa: E402
    _calmar, _econ_r_col, _sharpe, run_checks,
)

from paired_ab import _run  # noqa: E402  (byte-identical leg execution)

RISK_BAND = [0.005, 0.010, 0.020]
RISK_BASE = 0.010
BEAR_YEARS = [2008, 2020, 2022]
MIN_BEAR_SHORTS = 30          # gate-in (frozen)
SHARPE_TOL = 0.98             # Bar 1 (frozen)
MAXDD_SLACK = 2.0             # Bar 4 (frozen)


# ── pure bar evaluation (unit-tested; no I/O) ────────────────────────────────────

def evaluate_shorts_bars(
        *, n_bear_shorts, sharpe_on, sharpe_off, calmar_on, calmar_off,
        excess_on_band, excess_off_band, bear_windows,
        maxdd_on_full, maxdd_off_full,
        min_bear_shorts=MIN_BEAR_SHORTS, sharpe_tol=SHARPE_TOL,
        base_alpha=RISK_BASE, maxdd_slack=MAXDD_SLACK) -> dict:
    """Evaluate the FROZEN shorts bars from pre-computed scalars.

    ``excess_*_band`` : {alpha: excess-Sharpe vs SPY}. ``bear_windows`` : list of
    {year, maxdd_on, maxdd_off, excess_on, excess_off}. Returns a dict of per-bar
    booleans + ``ship``. Kept pure so the verdict logic is testable without a backtest.
    """
    gate = n_bear_shorts >= min_bear_shorts

    bar1 = (sharpe_on >= sharpe_off * sharpe_tol) and (calmar_on >= calmar_off)

    bar2_base = excess_on_band.get(base_alpha, float("nan")) >= excess_off_band.get(base_alpha, float("nan"))
    bar2_band = all(excess_on_band[a] >= excess_off_band[a] for a in excess_on_band)
    bar2 = bool(bar2_base and bar2_band)

    bw_ok = sum(1 for w in bear_windows
                if (w["maxdd_on"] <= w["maxdd_off"] and w["excess_on"] > w["excess_off"]))
    bar3 = bw_ok >= 2

    bar4 = maxdd_on_full <= maxdd_off_full + maxdd_slack

    ship = bool(gate and bar1 and bar2 and bar3 and bar4)
    return dict(gate=gate, bar1=bool(bar1), bar2=bar2, bar3=bar3, bar4=bar4,
                bear_windows_ok=bw_ok, ship=ship)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _ledger_df(trades) -> pd.DataFrame:
    """Trade objects → the flat ledger validate_shorts.run_checks expects."""
    rows = []
    for t in trades:
        rows.append(dict(
            direction=t.direction, exit_reason=t.exit_reason,
            r_multiple=float(t.r_multiple), effective_r=float(t.effective_r),
            size_mult=float(t.size_mult), borrow_annual_rate=float(t.borrow_annual_rate),
            market_regime=t.market_regime,
            entry_date=t.entry_date, exit_date=t.exit_date,
        ))
    return pd.DataFrame(rows)


def _maxdd_from_monthly(series: pd.Series) -> float:
    if series is None or len(series) == 0:
        return 0.0
    eq = series.sort_index().cumsum()
    return float((eq.cummax() - eq).max())


def _excess(monthly: pd.Series, spy_monthly: pd.Series, alpha: float) -> float:
    if monthly is None or len(monthly) < 2:
        return float("nan")
    per, sR, sP = align_strategy_benchmark(monthly, spy_monthly)
    if len(per) < 2:
        return float("nan")
    return information_ratio(sR * alpha, sP)


def _year_slice(monthly: pd.Series, year: int) -> pd.Series:
    if monthly is None or len(monthly) == 0:
        return pd.Series(dtype=float)
    mask = [str(i).startswith(f"{year}-") for i in monthly.index]
    return monthly[mask]


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Short-side validation (paired, borrow-honest, vs SPY)")
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10",
                    help="Frozen cache root (prices/ behavioral/ macro/ earnings_history/)")
    ap.add_argument("--tickers", nargs="+", default=None,
                    help="Restrict universe (smoke test only).")
    ap.add_argument("--save-ledgers", action="store_true",
                    help="Dump trades_shorts.csv / trades_longonly.csv to data/backtest_out/.")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = args.tickers or [t for t in wl.get("tier_a", wl.get("tickers", []))
                               if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True, cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history", macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral", start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)
    if uni.spy_df is None:
        print("  ✗ SPY not loaded from snapshot — cannot run the vs-SPY bars.")
        return

    cfg_lo = copy.deepcopy(base_cfg)
    cfg_lo.setdefault("signals", {})["allow_shorts"] = False
    cfg_sh = copy.deepcopy(base_cfg)
    cfg_sh.setdefault("signals", {})["allow_shorts"] = True

    print("  Running long-only leg…", flush=True)
    trades_lo = _run(uni, cfg_lo, settings, breakeven_trigger_r=1.0)
    print("  Running shorts-ON leg…", flush=True)
    trades_sh = _run(uni, cfg_sh, settings, breakeven_trigger_r=1.0)

    df_lo, df_sh = _ledger_df(trades_lo), _ledger_df(trades_sh)
    if args.save_ledgers:
        # Manual-study ledgers → docs/backtest_out/ (data/ is automated outputs only).
        out = _ROOT / "docs" / "backtest_out"
        out.mkdir(parents=True, exist_ok=True)
        df_lo.to_csv(out / "trades_longonly.csv", index=False)
        df_sh.to_csv(out / "trades_shorts.csv", index=False)
        print(f"  ledgers → {out}", flush=True)

    # ── ledger checks (1–6, borrow-honest check 4) ──
    print("\n" + "=" * 80)
    print("  SHORT-SIDE VALIDATION  (paired snapshot: long-only vs allow_shorts)")
    print("=" * 80)
    checks = run_checks(df_sh, baseline=df_lo)
    w = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  [{c.status:^4}] {c.name:<{w}}  {c.detail}")

    # ── economic scalars (effective_r) ──
    sh_on = _sharpe(df_sh, _econ_r_col(df_sh))
    sh_off = _sharpe(df_lo, _econ_r_col(df_lo))
    cal_on = _calmar(df_sh, _econ_r_col(df_sh))
    cal_off = _calmar(df_lo, _econ_r_col(df_lo))

    # ── vs-SPY (build_curve uses effective_r → borrow-honest) ──
    spy_monthly = month_end_returns(uni.spy_df["close"])
    ec_lo, ec_sh = build_curve(trades_lo), build_curve(trades_sh)
    m_lo, m_sh = ec_lo.monthly, ec_sh.monthly
    ex_on = {a: _excess(m_sh, spy_monthly, a) for a in RISK_BAND}
    ex_off = {a: _excess(m_lo, spy_monthly, a) for a in RISK_BAND}

    bear_windows = []
    for y in BEAR_YEARS:
        s_on, s_off = _year_slice(m_sh, y), _year_slice(m_lo, y)
        bear_windows.append(dict(
            year=y,
            maxdd_on=_maxdd_from_monthly(s_on), maxdd_off=_maxdd_from_monthly(s_off),
            excess_on=_excess(s_on, spy_monthly, RISK_BASE),
            excess_off=_excess(s_off, spy_monthly, RISK_BASE),
        ))

    n_bear_shorts = int(((df_sh["direction"] == "short") &
                         (df_sh["market_regime"].astype(str).str.startswith("BEAR"))).sum())

    verdict = evaluate_shorts_bars(
        n_bear_shorts=n_bear_shorts, sharpe_on=sh_on, sharpe_off=sh_off,
        calmar_on=cal_on, calmar_off=cal_off,
        excess_on_band=ex_on, excess_off_band=ex_off, bear_windows=bear_windows,
        maxdd_on_full=ec_sh.max_dd, maxdd_off_full=ec_lo.max_dd,
    )

    bar = "  " + "─" * 76
    print("\n  ECONOMIC (effective_r, borrow-honest) — Bar 1")
    print(bar)
    print(f"    Sharpe  ON {sh_on:+.3f}  vs OFF {sh_off:+.3f}  (need ≥ OFF×{SHARPE_TOL})")
    print(f"    Calmar  ON {cal_on:+.2f}   vs OFF {cal_off:+.2f}   (need ≥ OFF)")
    print(f"    totalR  ON {ec_sh.total_r:+.2f}  vs OFF {ec_lo.total_r:+.2f}   "
          f"|  maxDD ON {ec_sh.max_dd:.2f} vs OFF {ec_lo.max_dd:.2f}")

    print("\n  SPY-RELATIVE excess-Sharpe (α·strat_R − SPY_%) — Bar 2 (full) / Bar 3 (bear)")
    print(bar)
    print(f"    {'1R=':>6} | {'exSh ON':>8} {'exSh OFF':>9} {'ON≥OFF?':>8}")
    for a in RISK_BAND:
        print(f"    {a*100:>5.1f}% | {ex_on[a]:>+8.2f} {ex_off[a]:>+9.2f} "
              f"{('yes' if ex_on[a] >= ex_off[a] else 'no'):>8}"
              f"{'  ← base' if a == RISK_BASE else ''}")
    print(f"\n    {'BEAR yr':>8} | {'maxDD ON':>9} {'maxDD OFF':>10} {'exSh ON':>8} "
          f"{'exSh OFF':>9} {'window ok?':>10}")
    for bw in bear_windows:
        ok = (bw["maxdd_on"] <= bw["maxdd_off"]) and (bw["excess_on"] > bw["excess_off"])
        print(f"    {bw['year']:>8} | {bw['maxdd_on']:>9.2f} {bw['maxdd_off']:>10.2f} "
              f"{bw['excess_on']:>+8.2f} {bw['excess_off']:>+9.2f} {('YES' if ok else 'no'):>10}")

    def _mk(b): return "PASS" if b else "FAIL"
    print("\n" + "=" * 80)
    print("  PRE-REGISTERED VERDICT (shorts_validation_prereg.md — FROZEN)")
    print(bar)
    print(f"    Gate-in : {n_bear_shorts} BEAR shorts (need ≥{MIN_BEAR_SHORTS})   "
          f"[{'PASS' if verdict['gate'] else 'PENDING/underpowered'}]")
    print(f"    Bar 1   : economic Sharpe/Calmar on vs off          [{_mk(verdict['bar1'])}]")
    print(f"    Bar 2   : SPY-relative excess ON≥OFF (base + band)  [{_mk(verdict['bar2'])}]")
    print(f"    Bar 3   : bear-window insurance ({verdict['bear_windows_ok']}/3 ≥2 needed) "
          f"         [{_mk(verdict['bar3'])}]")
    print(f"    Bar 4   : full maxDD ON ≤ OFF+{MAXDD_SLACK:g}R              [{_mk(verdict['bar4'])}]")
    print(bar)
    if not verdict["gate"]:
        print("    >>> VERDICT: PENDING — too few BEAR shorts to judge (extend window/universe). <<<")
    elif verdict["ship"]:
        print("    >>> VERDICT: SHIP — all bars clear. Flip signals.allow_shorts:true (+ borrow filter). <<<")
    else:
        print("    >>> VERDICT: DO NOT SHIP — ≥1 bar FAILed. Shorts stay OFF (honest result). <<<")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
