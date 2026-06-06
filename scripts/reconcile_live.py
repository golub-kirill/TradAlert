#!/usr/bin/env python3
"""
Live-vs-backtest reconciliation — is the strategy tracking expectancy NOW?

Pulls fired entry signals from the live journal (`scan_results` + `scan_runs`),
replays each one forward against the cached price history under the shipped 25-bar
hard cap, and compares the realized R distribution to `backtest_trades` expectancy
(by regime). Flags drift > ±0.15 R/trade.

Scoring (the "Both" plan):
  • New rows (post-migration) carry stop_price/target_price/signal_type → scored exactly.
  • Old rows (geometry NULL) → stop/target reconstructed from close+atr+config (mode A).

A signal is *resolved* once it hits stop/target or reaches the cap; otherwise it's
*pending* (too recent — not enough forward bars yet). Reconciliation only judges
resolved signals; pending ones accrue as the live feed matures.

    python scripts/reconcile_live.py
    python scripts/reconcile_live.py --max-hold-days 25 --drift 0.15

Requires DB_* in config/secrets.env (same as the live scanner) and the price cache.
Read-only on the DB.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load DB_* (and other secrets) into the environment so persistence.db_conn sees
# them — mirrors main.py / run_backtest.py, which load this explicitly at startup.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


def _cfg():
    import yaml
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    sl = (c.get("signals", {}) or {}).get("stop_loss", {}) or {}
    ex = c.get("execution", {}) or {}
    return {
        "atr_mult": float(sl.get("atr_multiplier", 2.5)),
        "min_rr": float(sl.get("min_rr", 2.5)),
        "commission_r": float(ex.get("commission_r", 0.005)),
    }


def _ref_run(cur, bt_run_id):
    """Expectancy reference = the given backtest_runs id, else the latest.
    Always printed by the caller so the choice is visible."""
    rid = bt_run_id
    if rid is None:
        cur.execute("SELECT MAX(id) m FROM backtest_runs")
        row = cur.fetchone()
        rid = row["m"] if row else None
    if rid is None:
        return None
    cur.execute("SELECT id, start_date, end_date, trades_count, expectancy_r, "
                "win_rate, notes FROM backtest_runs WHERE id = %s", (rid,))
    return cur.fetchone()


def _fetch(conn, bt_run_id=None):
    cur = conn.cursor(dictionary=True)
    ref = _ref_run(cur, bt_run_id)
    if ref is None:
        cur.close()
        return [], {"__ALL__": (0.0, 0)}, None
    rid = ref["id"]
    cur.execute(
        "SELECT sr.id, sr.ticker, sr.signal_kind, sr.close, sr.atr, "
        "       sr.stop_price, sr.target_price, sr.signal_type, "
        "       r.created_at, r.market_regime "
        "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
        "WHERE sr.passed = 1 AND sr.signal_kind IN ('entry_long','entry_short') "
        "ORDER BY r.created_at, sr.ticker"
    )
    sigs = cur.fetchall()
    cur.execute(
        "SELECT market_regime, signal_type, COUNT(*) n, AVG(r_multiple) exp_r "
        "FROM backtest_trades WHERE run_id = %s GROUP BY market_regime, signal_type",
        (rid,),
    )
    exp = {(row["market_regime"], row["signal_type"]): (float(row["exp_r"]), int(row["n"]))
           for row in cur.fetchall()}
    cur.execute("SELECT AVG(r_multiple) e FROM backtest_trades WHERE run_id = %s", (rid,))
    overall = cur.fetchone()
    exp["__ALL__"] = (float(overall["e"]) if overall and overall["e"] is not None else 0.0, 0)
    cur.close()
    return sigs, exp, ref


def _replay(df, entry_idx, stop, target, is_short, max_hold, apply_stop, apply_target,
            apply_stop_s, apply_target_s):
    """Walk forward from entry_idx. Returns (exit_price, reason) or (None, 'pending')."""
    n = len(df)
    for k in range(entry_idx, n):
        bar = df.iloc[k]
        held = k - entry_idx
        lo, hi, op, cl = float(bar["low"]), float(bar["high"]), float(bar["open"]), float(bar["close"])
        if is_short:
            if hi >= stop:
                return apply_stop_s(stop, op), "stop"
            if lo <= target:
                return apply_target_s(target, op), "target"
        else:
            if lo <= stop:
                return apply_stop(stop, op), "stop"
            if hi >= target:
                return apply_target(target, op), "target"
        if held >= max_hold:
            return cl, "time_stop"
    return None, "pending"


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Live-vs-backtest reconciliation")
    ap.add_argument("--max-hold-days", type=int, default=25)
    ap.add_argument("--drift", type=float, default=0.15, help="alert threshold, R/trade")
    ap.add_argument("--bt-run-id", type=int, default=None,
                    help="backtest_runs.id to use as the expectancy reference "
                         "(default: the latest run; the chosen one is printed).")
    args = ap.parse_args()

    import pandas as pd
    from persistence.db_conn import connect
    from persistence.cache import load as cache_load
    from backtest.backtester import (apply_stop_fill, apply_target_fill,
                                      apply_stop_fill_short, apply_target_fill_short)

    cfg = _cfg()
    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env."); return
    try:
        sigs, exp, ref = _fetch(conn, args.bt_run_id)
    finally:
        conn.close()

    if ref is None:
        print("  No backtest_runs to reference — journal a backtest first "
              "(python -m backtest.run_backtest)."); return
    if not sigs:
        print("  No fired entry signals in scan_results yet — run main.py to build the feed.")
        return

    dates = [s["created_at"] for s in sigs]
    bt_all = exp.get("__ALL__", (0.0, 0))[0]
    print(f"\n  Live reconciliation  ·  {len(sigs)} fired entry signals  ·  "
          f"{min(dates):%Y-%m-%d} → {max(dates):%Y-%m-%d}  ·  cap {args.max_hold_days}d hard")
    print(f"  Expectancy reference: backtest_runs id={ref['id']} "
          f"({ref['start_date']}→{ref['end_date'] or 'latest'}, {ref['trades_count']} trades, "
          f"E[R] {bt_all:+.3f}, notes={ref['notes'] or '—'})\n")

    resolved = []          # (regime, signal_type, realized_r, reason, ticker, date)
    pending = 0
    errors = 0
    reconstructed = 0
    for s in sigs:
        try:
            df = cache_load(s["ticker"])
        except Exception:
            errors += 1
            continue
        D = pd.Timestamp(s["created_at"]).normalize()
        entry_idx = int(df.index.searchsorted(D, side="right"))  # first bar strictly after scan date = T+1
        if entry_idx >= len(df):
            pending += 1
            continue
        entry = float(df.iloc[entry_idx]["open"])
        is_short = s["signal_kind"] == "entry_short"
        close_d = float(s["close"]) if s["close"] is not None else entry
        atr = float(s["atr"]) if s["atr"] is not None else 0.0

        # exact geometry when stored; else reconstruct from close+atr+config (mode A)
        if s["stop_price"] is not None and s["target_price"] is not None:
            stop, target = float(s["stop_price"]), float(s["target_price"])
        else:
            reconstructed += 1
            if is_short:
                stop = close_d + atr * cfg["atr_mult"]
                target = close_d - (stop - close_d) * cfg["min_rr"]
            else:
                stop = close_d - atr * cfg["atr_mult"]
                target = close_d + (close_d - stop) * cfg["min_rr"]

        risk = (stop - entry) if is_short else (entry - stop)
        if risk <= 0:
            errors += 1
            continue

        exit_price, reason = _replay(df, entry_idx, stop, target, is_short, args.max_hold_days,
                                     apply_stop_fill, apply_target_fill,
                                     apply_stop_fill_short, apply_target_fill_short)
        if exit_price is None:
            pending += 1
            continue
        r = ((entry - exit_price) / risk) if is_short else ((exit_price - entry) / risk)
        r -= cfg["commission_r"]
        st = s["signal_type"] or "momentum"
        resolved.append((s["market_regime"], st, r, reason, s["ticker"], D.date()))

    print(f"  Resolved: {len(resolved)}   Pending (too recent): {pending}   "
          f"Errors/skipped: {errors}   (geometry reconstructed for {reconstructed})")
    if not resolved:
        print("\n  ⚠ Nothing matured yet — the live feed is too young to score outcomes.")
        print("    Keep the scanner running daily; rerun this once signals age ~25 trading days.\n")
        return

    # aggregate by regime
    by_reg = defaultdict(list)
    for reg, st, r, *_ in resolved:
        by_reg[(reg, st)].append(r)
    allr = [r for _, _, r, *_ in resolved]
    live_all = sum(allr) / len(allr)

    print("\n" + "=" * 78)
    print(f"  {'Regime / type':<24} {'live n':>6} {'live E[R]':>9} {'bt E[R]':>8} "
          f"{'drift':>7}  flag")
    print("  " + "-" * 74)
    for key in sorted(by_reg):
        rs = by_reg[key]
        le = sum(rs) / len(rs)
        be = exp.get(key, (None, 0))[0]
        if be is None:
            print(f"  {key[0] + '/' + key[1]:<24} {len(rs):>6} {le:>+9.3f} {'  n/a':>8} {'':>7}")
            continue
        drift = le - be
        flag = "  ⚠ DRIFT" if abs(drift) > args.drift else "  ok"
        print(f"  {key[0] + '/' + key[1]:<24} {len(rs):>6} {le:>+9.3f} {be:>+8.3f} {drift:>+7.3f}{flag}")
    print("  " + "-" * 74)
    drift_all = live_all - bt_all
    print(f"  {'ALL':<24} {len(allr):>6} {live_all:>+9.3f} {bt_all:>+8.3f} {drift_all:>+7.3f}"
          f"{'  ⚠ DRIFT' if abs(drift_all) > args.drift else '  ok'}")
    # win rate + exit mix
    wr = 100 * sum(1 for r in allr if r > 0) / len(allr)
    mix = defaultdict(int)
    for _reg, _st, _r, reason, _tk, _dt in resolved:
        mix[reason] += 1
    print(f"  live WR {wr:.0f}%   exits: " + ", ".join(f"{k}={v}" for k, v in sorted(mix.items())))
    print("=" * 78)
    if abs(drift_all) > args.drift:
        print(f"\n  ⚠ Live is drifting {drift_all:+.3f} R/trade from backtest expectancy "
              f"(|drift| > {args.drift}). Investigate before trusting the edge live.\n")
    else:
        print(f"\n  ✓ Live tracking backtest within ±{args.drift} R/trade on resolved signals.\n")


if __name__ == "__main__":
    main()
