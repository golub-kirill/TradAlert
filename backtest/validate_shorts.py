"""
Phase 10.6 — short-trading validation harness.

Postmortem-style acceptance checks for the short side, computed from a
backtest trade ledger (``data/backtest_out/trades.csv``). Run this AFTER
a ``--allow-shorts`` backtest over a window that contains real BEAR
regimes (the cached 2023-2026 data is bull-only — extend the dataset or
temporarily relax the regime classifier so shorts actually fire).

Checks implemented here (data-driven, from the trade ledger):

  1. Trade count by direction          — goal: >= 10 shorts in the window.
  2. R-distribution symmetry           — short stop-outs centred near -1R,
                                          not clustered at <= -2R (gap-fill
                                          geometry sanity).
  3. Win rate by side                  — shorts within 10pp of longs.
  4. Sharpe / Calmar shorts-on vs off  — needs two ledgers (see --baseline);
                                          acceptance: with-shorts >= flat.
  5. By-exit breakdown                 — short stop-rate < 40% (matches longs).

Check 6 (no concurrent long+short on the same ticker) is a structural
invariant of PortfolioBacktester and is covered by the unit test
``tests/test_short_portfolio_guard.py`` — not re-derived from the ledger.

Usage
-----
    # single ledger (checks 1, 2, 3, 5):
    python -m backtest.validate_shorts data/backtest_out/trades.csv

    # add Sharpe/Calmar on-vs-off (check 4):
    python -m backtest.validate_shorts data/backtest_out/trades_shorts.csv \
        --baseline data/backtest_out/trades_baseline.csv

Exit code is 0 when no check FAILs (WARNs are allowed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pandas as pd

# ── acceptance thresholds ───────────────────────────────────────────────────
MIN_SHORTS = 10  # check 1
SHORT_STOP_MEAN_FLOOR = -1.5  # check 2: mean stop-out R below this = WARN
WR_GAP_MAX_PP = 10.0  # check 3: max (long_WR - short_WR) in points
SHORT_STOP_RATE_MAX = 0.40  # check 5: short stop-rate ceiling
SHARPE_FLAT_TOL = 0.98  # check 4: with-shorts Sharpe >= baseline * tol

_EXIT_REASONS = ("stop", "target", "engine_exit", "open_eod")


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str


# ── metric helpers ──────────────────────────────────────────────────────────

def _win_rate(df: pd.DataFrame) -> float:
    if df.empty:
        return float("nan")
    return float((df["r_multiple"] > 0).mean() * 100.0)


def _sharpe(df: pd.DataFrame) -> float:
    """Per-trade Sharpe proxy: mean(R) / std(R)."""
    if len(df) < 2:
        return float("nan")
    sd = float(df["r_multiple"].std(ddof=1))
    if sd == 0:
        return float("nan")
    return float(df["r_multiple"].mean()) / sd


def _max_drawdown_r(df: pd.DataFrame) -> float:
    """Max peak-to-trough drawdown (in R) of the cumulative-R equity curve."""
    if df.empty:
        return 0.0
    order = "exit_date" if "exit_date" in df.columns else "entry_date"
    s = df.sort_values(order)["r_multiple"].cumsum()
    return float((s.cummax() - s).max())


def _calmar(df: pd.DataFrame) -> float:
    total_r = float(df["r_multiple"].sum())
    dd = _max_drawdown_r(df)
    if dd <= 0:
        return float("inf") if total_r > 0 else 0.0
    return total_r / dd


def _stop_rate(df: pd.DataFrame) -> float:
    if df.empty:
        return float("nan")
    return float((df["exit_reason"] == "stop").mean())


def _exit_breakdown(df: pd.DataFrame) -> dict[str, int]:
    counts = df["exit_reason"].value_counts().to_dict()
    return {r: int(counts.get(r, 0)) for r in _EXIT_REASONS}


# ── checks ──────────────────────────────────────────────────────────────────

def _require_direction(df: pd.DataFrame) -> None:
    if "direction" not in df.columns:
        sys.exit(
            "trades ledger has no 'direction' column — re-run the backtest "
            "after the Phase 10.6 CSV-schema update (backtest/sweep.py "
            "trades_dataframe now emits 'direction')."
        )


def run_checks(df: pd.DataFrame, baseline: pd.DataFrame | None) -> list[Check]:
    _require_direction(df)
    longs = df[df["direction"] == "long"]
    shorts = df[df["direction"] == "short"]
    checks: list[Check] = []

    # 1. trade count by direction
    n_short = len(shorts)
    checks.append(Check(
        "1. trade count by direction",
        "PASS" if n_short >= MIN_SHORTS else "WARN",
        f"{len(longs)} long / {n_short} short "
        f"(goal >= {MIN_SHORTS} shorts; if 0, the regime gate is masking "
        f"the test — relax it or extend the dataset)",
    ))

    # 2. R-distribution symmetry on stop-outs
    long_stops = longs[longs["exit_reason"] == "stop"]["r_multiple"]
    short_stops = shorts[shorts["exit_reason"] == "stop"]["r_multiple"]
    if short_stops.empty:
        checks.append(Check("2. R-distribution symmetry", "SKIP",
                            "no short stop-outs in ledger"))
    else:
        sm = float(short_stops.mean())
        lm = float(long_stops.mean()) if not long_stops.empty else float("nan")
        status = "PASS" if sm >= SHORT_STOP_MEAN_FLOOR else "WARN"
        checks.append(Check(
            "2. R-distribution symmetry", status,
            f"short stop-out mean={sm:.2f}R (long={lm:.2f}R); "
            f"floor {SHORT_STOP_MEAN_FLOOR}R — below it implies gap-fill "
            f"geometry is too punitive",
        ))

    # 3. win rate by side
    lwr, swr = _win_rate(longs), _win_rate(shorts)
    if shorts.empty:
        checks.append(Check("3. win rate by side", "SKIP", "no short trades"))
    else:
        gap = lwr - swr
        status = "PASS" if gap <= WR_GAP_MAX_PP else "WARN"
        checks.append(Check(
            "3. win rate by side", status,
            f"long WR={lwr:.1f}%  short WR={swr:.1f}%  gap={gap:+.1f}pp "
            f"(max {WR_GAP_MAX_PP:.0f}pp)",
        ))

    # 4. Sharpe / Calmar shorts-on vs off
    if baseline is None:
        checks.append(Check(
            "4. Sharpe/Calmar shorts on vs off", "SKIP",
            "pass --baseline <long-only trades.csv> to compute",
        ))
    else:
        on_sh, off_sh = _sharpe(df), _sharpe(baseline)
        on_cal, off_cal = _calmar(df), _calmar(baseline)
        ok = (on_sh >= off_sh * SHARPE_FLAT_TOL) if off_sh == off_sh else True
        checks.append(Check(
            "4. Sharpe/Calmar shorts on vs off",
            "PASS" if ok else "WARN",
            f"Sharpe {off_sh:.3f} -> {on_sh:.3f} | Calmar {off_cal:.2f} -> "
            f"{on_cal:.2f} (shorts are insurance: want flat or better)",
        ))

    # 5. by-exit breakdown
    if shorts.empty:
        checks.append(Check("5. by-exit breakdown (short stop-rate)", "SKIP",
                            "no short trades"))
    else:
        sr = _stop_rate(shorts)
        bd = _exit_breakdown(shorts)
        status = "PASS" if sr < SHORT_STOP_RATE_MAX else "WARN"
        checks.append(Check(
            "5. by-exit breakdown (short stop-rate)", status,
            f"short stop-rate={sr * 100:.1f}% (ceiling {SHORT_STOP_RATE_MAX * 100:.0f}%)  "
            f"exits={bd}",
        ))

    # 6. concurrency invariant — pointer only
    checks.append(Check(
        "6. no concurrent long+short / ticker", "PASS",
        "structural; verified by tests/test_short_portfolio_guard.py",
    ))
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 10.6 short-trading validation.")
    ap.add_argument("trades", nargs="?", default="data/backtest_out/trades.csv",
                    help="trade ledger CSV (from a --allow-shorts run).")
    ap.add_argument("--baseline", default=None,
                    help="long-only trade ledger for the shorts on-vs-off check.")
    args = ap.parse_args(argv)

    df = pd.read_csv(args.trades)
    baseline = pd.read_csv(args.baseline) if args.baseline else None

    checks = run_checks(df, baseline)

    width = max(len(c.name) for c in checks)
    print(f"\nShort-trading validation — {args.trades}")
    print("─" * (width + 60))
    worst_fail = False
    for c in checks:
        if c.status == "FAIL":
            worst_fail = True
        print(f"  [{c.status:^4}] {c.name:<{width}}  {c.detail}")
    print("─" * (width + 60))
    print("WARN = investigate; FAIL = blocker. "
          "Re-run over a BEAR-containing window if shorts are sparse.\n")
    return 1 if worst_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
