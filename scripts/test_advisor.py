#!/usr/bin/env python3
"""
Advisor plumbing smoke test — random backtest trade(s) → LLM verdict.

Feeds real journaled trade setups through the full advisor chain (news fetch →
Ollama → formatted note) and prints each verdict next to the realized R, so you
can eyeball the advisor's judgment on real signal shapes. Forces
advisor.enabled for the run regardless of settings.yaml.

⚠ NOT an eval. Ticker news and macro context are fetched TODAY while the trade
is up to a year old — the news axis is anachronistic (post-entry or irrelevant),
so verdict-vs-outcome here is plumbing/judgment color, never evidence. The
honest measurement is the prospective live journal (scan_results.advisor_note
scored against realized R after >=30-50 live verdicts).

Usage:
    python scripts/test_advisor.py
    python scripts/test_advisor.py --seed 42 --count 5     # reproducible sample
    python scripts/test_advisor.py --model qwen3:8b --no-macro
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

def _min_rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return reward / risk if risk > 0 else 0.0


def _verdict_label(note: str) -> str:
    # "Disagree" first: case-insensitive matching would hit its "agree" tail.
    for label in ("Disagree", "Agree", "Flag"):
        if label in note:
            return label.upper()
    return "—"


def _actual_label(r: float | None) -> str:
    if r is None:
        return "N/A"
    return "PROFIT" if r > 0 else ("FLAT" if r == 0 else "LOSS")


def _fetch_trades(seed: int | None, count: int) -> list[dict]:
    """Random qualifying trades from the last 365 days. [] on DB failure.

    Reproducibility lives in SQL: MySQL's RAND(N) is deterministic for a given
    seed (a Python random.seed() would have no effect on ORDER BY RAND()).
    """
    from persistence.db_conn import connect

    base = """
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
        ORDER BY {order}
        LIMIT %(n)s
    """
    if seed is not None:
        sql = base.format(order="RAND(%(seed)s)")
        params = {"seed": seed, "n": count}
    else:
        sql = base.format(order="RAND()")
        params = {"n": count}

    conn = None
    try:
        conn = connect()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()
    except Exception as exc:
        print(f"  DB query failed ({exc}). Set DB_* in config/secrets.env "
              "and journal a backtest first (python -m backtest.run_backtest).")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _print_trade(trade: dict, signal, note: str) -> None:
    entry = float(trade["entry_price"])
    # Raw per-unit R is the outcome the advisor's read maps to; effective_r
    # folds in size_mult/borrow (sizing layer), shown alongside when it differs.
    r_raw = float(trade["r_multiple"]) if trade["r_multiple"] is not None else None
    r_eff = float(trade["effective_r"]) if trade["effective_r"] is not None else None

    result_str = f"{r_raw:+.2f}R" if r_raw is not None else "N/A"
    if r_eff is not None and r_raw is not None and abs(r_eff - r_raw) > 1e-4:
        result_str += f" (effective {r_eff:+.2f}R)"

    print("  " + "─" * 56)
    print(f"  {trade['ticker']:<8} {trade['direction'].upper():<6} "
          f"{trade['signal_type'] or 'setup':<16}  "
          f"{trade['entry_date']} → {trade['exit_date']}")
    print(f"  Entry   {entry:<10.2f} Stop  {signal.stop_price:<10.2f} "
          f"Target {signal.target_price:<10.2f} R:R {signal.min_rr:.1f}")
    print(f"  Regime  {trade['market_regime']:<20}  "
          f"Trend  {trade['ticker_trend']:<10}")
    print(f"  Exit    {trade['exit_reason'] or '—':<20}  "
          f"Result  {result_str}")
    if note:
        print(f"  Advisor  {note}")
    else:
        print("  Advisor  (no response — is Ollama running?)")
    print(f"  Verdict  {_verdict_label(note):<12}  Actual  {_actual_label(r_raw)}")


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="AI advisor vs backtest trades (plumbing smoke test)")
    ap.add_argument("--seed", type=int, default=None,
                    help="MySQL RAND(seed) for reproducible trade selection")
    ap.add_argument("--count", type=int, default=1,
                    help="number of random trades to review [1]")
    ap.add_argument("--model", default=None,
                    help="override advisor.model for this run (e.g. qwen3:8b)")
    ap.add_argument("--no-macro", action="store_true",
                    help="skip the macro-context summarization (faster)")
    args = ap.parse_args()

    import yaml

    from core.types import SignalResult
    from core.advisor import build_advisor_context, advise_signal

    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}

    # Force-enable for the test run — the shipped default is OFF, and a smoke
    # test that silently exercises the disabled path tests nothing.
    settings.setdefault("advisor", {})["enabled"] = True
    if args.model:
        settings["advisor"]["model"] = args.model
    if args.no_macro:
        settings.setdefault("news", {})["macro_summarization"] = False

    trades = _fetch_trades(args.seed, max(1, args.count))
    if not trades:
        print("  No qualifying backtest trades in the last 365 days "
              "(need stop/target/trend/regime).")
        return

    ctx = build_advisor_context(settings)
    print()
    print(f"  Model    {ctx.model}   macro context: "
          f"{'yes' if ctx.market_context else 'no'}")

    for trade in trades:
        entry = float(trade["entry_price"])
        stop = float(trade["initial_stop"])
        target = float(trade["initial_target"])

        signal = SignalResult(
            passed=True,
            direction=trade["direction"],
            signal_type=trade["signal_type"] or "momentum",
            stop_price=stop,
            target_price=target,
            min_rr=_min_rr(entry, stop, target),
            market_regime=trade["market_regime"] or "",
            ticker_trend=trade["ticker_trend"] or "",
            reason=f"{trade['signal_type'] or 'setup'} signal",
        )
        note = advise_signal(trade["ticker"], signal, ctx)
        _print_trade(trade, signal, note)

    print("  " + "─" * 56)
    print("  ⚠ news/macro are PRESENT-DAY vs a historical entry — plumbing "
          "smoke test only, not an eval of advisor value.")
    print()


if __name__ == "__main__":
    main()
