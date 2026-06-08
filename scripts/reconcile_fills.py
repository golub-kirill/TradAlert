#!/usr/bin/env python3
"""
Real-fill reconciliation — is the strategy tracking expectancy on ACTUAL trades?

The sibling `reconcile_live.py` replays *fired signals* through cached prices, so
it measures signal fidelity (a delayed backtest). This one is the real meter: it
reads **actual fills** from the `positions` table (logged via `position_CLI.py`),
computes each closed trade's realized R from the recorded entry/stop/exit, and
compares the realized distribution to `backtest_trades` expectancy by direction.

Realized R uses the initial recorded stop as the risk unit, matching the
backtester:  long  R = (exit - entry) / (entry - stop)
             short R = (entry - exit) / (stop - entry)
A per-trade commission (R units) is deducted to match the backtest convention
(recorded fills are execution prices, gross of commission). Flags drift > ±0.15 R.

Closed positions (exit_date set) are scored. Open positions are listed as
carried risk but not scored. A closed position with no recorded stop can't be
scored (no risk unit) and is counted separately.

    python scripts/reconcile_fills.py
    python scripts/reconcile_fills.py --drift 0.10 --bt-run-id 8

Requires DB_* in config/secrets.env (same as the live scanner). Read-only on the DB.
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


# ── pure R math (unit-tested; no DB) ────────────────────────────────────────

# Single source for the risk-unit geometry — shared with the open-position guard
# (position_manager.validate_open) so a stop the guard rejects can't be scored here.
from core.position_manager import risk_unit as _risk  # noqa: E402


def _r_multiple(side: str, entry: float, stop: float, exit_price: float) -> float | None:
    """Realized R of a closed trade, gross of commission. None when the risk
    unit is non-positive (missing/degenerate stop) and R is undefined."""
    risk = _risk(side, entry, stop)
    if risk <= 0:
        return None
    gain = (exit_price - entry) if side == "long" else (entry - exit_price)
    return gain / risk


def reconcile(closed, commission_r: float = 0.0):
    """Score closed positions into realized R, bucketed by side.

    `closed` is an iterable of objects with .side/.entry_price/.stop_price/
    .exit_price/.ticker/.exit_date (the position_manager.Position shape).

    Returns a dict:
        by_side   : {side: [r, ...]}          scored realized R per side
        scored    : [(side, r, ticker, exit_date), ...]
        no_stop   : int   closed but stop_price is None → unscorable
        bad_risk  : int   stop present but risk <= 0 (degenerate geometry)
    """
    by_side: dict[str, list[float]] = defaultdict(list)
    scored: list[tuple] = []
    no_stop = 0
    bad_risk = 0
    for p in closed:
        if p.stop_price is None:
            no_stop += 1
            continue
        r = _r_multiple(p.side, float(p.entry_price), float(p.stop_price),
                        float(p.exit_price))
        if r is None:
            bad_risk += 1
            continue
        r -= commission_r
        by_side[p.side].append(r)
        scored.append((p.side, r, p.ticker, p.exit_date))
    return {"by_side": dict(by_side), "scored": scored,
            "no_stop": no_stop, "bad_risk": bad_risk}


# ── DB I/O ──────────────────────────────────────────────────────────────────

def _cfg_commission_r() -> float:
    import yaml
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    ex = c.get("execution", {}) or {}
    return float(ex.get("commission_r", 0.005))


def _load_expectancy(conn, bt_run_id):
    """Return (ref_row, exp) where exp maps side -> (E[R], n) plus '__ALL__'.
    ref_row is the backtest_runs row used as the reference, or None."""
    from backtest.db import reference_run, trade_r_column
    cur = conn.cursor(dictionary=True)
    # Provenance-aware reference (latest scoring-OFF run, matching live) and
    # effective_r aggregation (matches backtest_runs.total_r once sizing is active).
    ref = reference_run(cur, bt_run_id)
    if ref is None:
        cur.close()
        return None, {"__ALL__": (0.0, 0)}
    rid = ref["id"]
    rcol = trade_r_column(cur)
    cur.execute(f"SELECT direction, AVG({rcol}) e, COUNT(*) n "
                "FROM backtest_trades WHERE run_id = %s GROUP BY direction", (rid,))
    exp = {row["direction"]: (float(row["e"]), int(row["n"]))
           for row in cur.fetchall() if row["e"] is not None}
    cur.execute(f"SELECT AVG({rcol}) e, COUNT(*) n FROM backtest_trades WHERE run_id = %s",
                (rid,))
    overall = cur.fetchone()
    exp["__ALL__"] = (float(overall["e"]) if overall and overall["e"] is not None else 0.0,
                      int(overall["n"]) if overall else 0)
    cur.close()
    return ref, exp


# ── report ──────────────────────────────────────────────────────────────────

def _print_report(result, exp, ref, open_positions, drift, commission_r):
    scored = result["scored"]
    by_side = result["by_side"]
    bt_all = exp.get("__ALL__", (0.0, 0))[0]

    print(f"\n  Real-fill reconciliation  ·  {len(scored)} closed scored  ·  "
          f"{len(open_positions)} open (carried)  ·  commission {commission_r:.3f} R/trade")
    if ref is not None:
        print(f"  Expectancy reference: backtest_runs id={ref['id']} "
              f"({ref['start_date']}→{ref['end_date'] or 'latest'}, {ref['trades_count']} trades, "
              f"E[R] {bt_all:+.3f}, notes={ref['notes'] or '—'})")
    skipped = []
    if result["no_stop"]:
        skipped.append(f"{result['no_stop']} no-stop")
    if result["bad_risk"]:
        skipped.append(f"{result['bad_risk']} bad-geometry")
    if skipped:
        print(f"  Unscored closed: {', '.join(skipped)} (can't define a risk unit)")

    if open_positions:
        print("\n  Open (carried, not scored):")
        for p in open_positions:
            stop_s = f"{float(p.stop_price):.4f}" if p.stop_price is not None else "—"
            print(f"    {p.ticker:<10} {p.side:<5} entry {float(p.entry_price):>10.4f} "
                  f"stop {stop_s:>10}  since {p.entry_date}")

    if not scored:
        print("\n  ⚠ No closed positions with a usable stop yet — nothing to score.")
        print("    Log fills with `position_CLI.py open/close` (record --stop), then rerun.\n")
        return

    allr = [r for _s, r, *_ in scored]
    live_all = sum(allr) / len(allr)
    print("\n" + "=" * 70)
    print(f"  {'Direction':<12} {'live n':>6} {'live E[R]':>9} {'bt E[R]':>8} {'drift':>7}  flag")
    print("  " + "-" * 66)
    for side in sorted(by_side):
        rs = by_side[side]
        le = sum(rs) / len(rs)
        be = exp.get(side, (None, 0))[0]
        if be is None:
            print(f"  {side:<12} {len(rs):>6} {le:>+9.3f} {'  n/a':>8} {'':>7}")
            continue
        drift_s = le - be
        flag = "  ⚠ DRIFT" if abs(drift_s) > drift else "  ok"
        print(f"  {side:<12} {len(rs):>6} {le:>+9.3f} {be:>+8.3f} {drift_s:>+7.3f}{flag}")
    print("  " + "-" * 66)
    drift_all = live_all - bt_all
    print(f"  {'ALL':<12} {len(allr):>6} {live_all:>+9.3f} {bt_all:>+8.3f} {drift_all:>+7.3f}"
          f"{'  ⚠ DRIFT' if abs(drift_all) > drift else '  ok'}")
    wr = 100 * sum(1 for r in allr if r > 0) / len(allr)
    tot = sum(allr)
    print(f"  live WR {wr:.0f}%   total {tot:+.2f} R")
    print("=" * 70)
    if abs(drift_all) > drift:
        print(f"\n  ⚠ Real fills drift {drift_all:+.3f} R/trade from backtest expectancy "
              f"(|drift| > {drift}). Investigate before trusting the edge live.\n")
    else:
        print(f"\n  ✓ Real fills tracking backtest within ±{drift} R/trade.\n")


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Real-fill (positions) reconciliation")
    ap.add_argument("--drift", type=float, default=0.15, help="alert threshold, R/trade")
    ap.add_argument("--bt-run-id", type=int, default=None,
                    help="backtest_runs.id to use as the expectancy reference "
                         "(default: the latest run; the chosen one is printed).")
    ap.add_argument("--commission-r", type=float, default=None,
                    help="per-trade commission in R deducted from each realized R "
                         "(default: execution.commission_r from filters.yaml).")
    args = ap.parse_args()

    from persistence.db_conn import connect
    from core import position_manager as pm

    commission_r = args.commission_r if args.commission_r is not None else _cfg_commission_r()

    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env."); return
    try:
        ref, exp = _load_expectancy(conn, args.bt_run_id)
    finally:
        conn.close()

    if ref is None:
        print("  No backtest_runs to reference — journal a backtest first "
              "(python -m backtest.run_backtest).")
        return

    positions = pm.list_all()
    closed = [p for p in positions if not p.is_open]
    open_positions = [p for p in positions if p.is_open]

    result = reconcile(closed, commission_r)
    _print_report(result, exp, ref, open_positions, args.drift, commission_r)


if __name__ == "__main__":
    main()
