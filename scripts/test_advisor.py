#!/usr/bin/env python3
"""
Random backtest trade → AI advisor comparison.

Picks one random trade from the last 365 days (with valid stop/target/trend),
feeds its setup to the LLM advisor, and prints the verdict next to the actual
R outcome so you can judge whether the advisor would have helped or misled.

Usage:
    python scripts/test_advisor.py
    python scripts/test_advisor.py --seed 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


# ── helpers ──────────────────────────────────────────────────────────

def _r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float:
    risk = abs(entry - stop)
    gain = (exit_price - entry) if side == "long" else (entry - exit_price)
    return gain / risk if risk > 0 else 0.0


def _min_rr(side: str, entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return reward / risk if risk > 0 else 0.0


def _verdict_label(note: str) -> str:
    for label in ("Agree", "Disagree", "Flag"):
        if label in note:
            return label.upper()
    return "—"


def _actual_label(r: float | None) -> str:
    if r is None:
        return "N/A"
    return "PROFIT" if r > 0 else "LOSS"


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="AI advisor vs backtest trade")
    ap.add_argument("--seed", type=int, default=None,
                    help="random seed for reproducible trade selection")
    args = ap.parse_args()

    import random
    import yaml

    from persistence.db_conn import connect
    from core.types import SignalResult
    from core.advisor import build_advisor_context, advise_signal

    seed = args.seed
    if seed is not None:
        random.seed(seed)

    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    try:
        conn = connect()
    except Exception as exc:
        print(f"  DB connect failed ({exc}). Set DB_* in config/secrets.env.")
        return

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ticker, direction, signal_type,
               entry_date, exit_date,
               entry_price, initial_stop, initial_target,
               r_multiple, effective_r, exit_reason,
               market_regime, ticker_trend
        FROM backtest_trades
        WHERE entry_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
          AND initial_stop IS NOT NULL
          AND initial_target IS NOT NULL
          AND ticker_trend IS NOT NULL AND ticker_trend != ''
          AND market_regime IS NOT NULL
        ORDER BY RAND()
        LIMIT 1
    """)
    trade = cur.fetchone()
    cur.close()
    conn.close()

    if trade is None:
        print("  No qualifying backtest trades in the last 365 days "
              "(need stop/target/trend/regime).")
        return

    # ── build mock signal ────────────────────────────────────────────
    entry = float(trade["entry_price"])
    stop = float(trade["initial_stop"])
    target = float(trade["initial_target"])
    side = trade["direction"]

    signal = SignalResult(
        passed=True,
        direction=side,
        signal_type=trade["signal_type"] or "momentum",
        stop_price=stop,
        target_price=target,
        min_rr=_min_rr(side, entry, stop, target),
        market_regime=trade["market_regime"] or "",
        ticker_trend=trade["ticker_trend"] or "",
        reason=f"{trade['signal_type'] or 'setup'} signal",
    )

    # ── run advisor ──────────────────────────────────────────────────
    ctx = build_advisor_context(settings)
    note = advise_signal(trade["ticker"], signal, ctx)

    # ── actual result ────────────────────────────────────────────────
    r = float(trade["effective_r"]) if trade["effective_r"] is not None else (
        float(trade["r_multiple"]) if trade["r_multiple"] is not None else None)

    # ── print report ─────────────────────────────────────────────────
    vl = _verdict_label(note)
    al = _actual_label(r)

    if r is not None:
        result_str = f"{r:+.2f}R"
    else:
        result_str = "N/A"

    print()
    print("  " + "─" * 56)
    print(f"  {trade['ticker']:<8} {trade['direction'].upper():<6} "
          f"{trade['signal_type'] or 'setup':<16}  "
          f"{trade['entry_date']} → {trade['exit_date']}")
    print(f"  Regime  {trade['market_regime']:<20}  "
          f"Trend  {trade['ticker_trend']:<10}  "
          f"R:R  {signal.min_rr:.1f}")
    print(f"  Exit    {trade['exit_reason'] or '—':<20}  "
          f"Result  {result_str}")
    print("  " + "─" * 56)
    if note:
        print(f"  Advisor  {note}")
    else:
        print("  Advisor  (no response — Ollama down or disabled)")
    print(f"  Verdict  {vl:<12}  Actual  {al:<10}  "
          f"{'✓' if (vl == 'AGREE' and r and r > 0) or (vl == 'DISAGREE' and r and r < 0) else '✗'}")
    print("  " + "─" * 56)
    print()


if __name__ == "__main__":
    main()
