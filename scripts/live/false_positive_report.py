#!/usr/bin/env python3
"""
Live false-positive report — the DOA / stalled / gave-back taxonomy on REAL fills.

`reconcile_fills.py` answers "are live fills tracking backtest expectancy?" (realized
R vs the reference run). This answers the sibling question the FP-anatomy research
raised: WHAT KIND of trades are the losers? Do live entries fail because the signal
never worked (dead-on-arrival — the dominant backtest false positive, ~32% of alerts),
or because a real move was given back (an exit problem)?

For each CLOSED position with a usable initial stop it walks the cached price path over
the hold window [entry_date, exit_date] and records, in initial-stop-R units:
    mfe_r   max favorable excursion  — how far it ever ran in your favor
    mae_r   max adverse excursion    — how far against (<= 0)
then buckets the outcome exactly like the backtest lens:
    winner      realized R > 0
    DOA         loss, MFE < 0.25R       — never followed through (the false positive)
    stalled     loss, 0.25R <= MFE < 1R
    gave_back   loss, MFE >= 1R         — reached profit then lost it (an exit problem)

The same taxonomy is printed for the backtest reference run (backtest_trades already
stores mfe_r/mae_r) so live can be read against it as the journal matures (Phase 5).

Excursions are computed from cached bars each run — the whole history backfills, no
schema change. The MFE/MAE use the hold-window high/low; on the entry bar this can
overstate slightly (the intraday fill time is unknown), which only ever moves a trade
OUT of the DOA bucket, so the DOA share is a floor, not inflated. Read-only on the DB
and the parquet cache.

    python scripts/live/false_positive_report.py
    python scripts/live/false_positive_report.py --min-ticker 4 --bt-run-id 26
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load DB_* into the environment so persistence.db_conn sees them — mirrors the
# sibling reconcilers and main.py, which load secrets.env explicitly at startup.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


# ── taxonomy thresholds (single source; match the FP-anatomy report + backtest lens) ──

DOA_MFE_R: float = 0.25    # favorable excursion below this on a loser = "never worked"
WORKED_MFE_R: float = 1.0  # a loser that reached >= this ran, then gave it back
BUCKETS = ("winner", "DOA", "stalled", "gave_back")


# ── pure classification / excursion math (unit-tested; no DB, no I/O) ────────────

def classify(mfe_r: float | None, r: float) -> str:
    """Bucket a resolved trade from its realized R and max-favorable excursion (R).

    A profitable trade is a ``winner`` regardless of path. A loser is graded by how
    far it ever ran in your favor: ``DOA`` (never worked), ``stalled`` (a nibble),
    or ``gave_back`` (reached >=1R, then lost). ``mfe_r is None`` (no risk unit)
    can't be graded → ``unscored``.
    """
    if r > 0:
        return "winner"
    if mfe_r is None:
        return "unscored"
    if mfe_r < DOA_MFE_R:
        return "DOA"
    if mfe_r < WORKED_MFE_R:
        return "stalled"
    return "gave_back"


def excursions(bars, entry_price: float, initial_stop: float, side: str):
    """Max favorable/adverse excursion of a held position over ``bars``, in R units.

    ``bars`` is the OHLCV slice for the hold window (DatetimeIndex; ``high``/``low``
    columns). R unit is the initial-stop risk (``entry - stop`` long, ``stop - entry``
    short). Returns ``(mfe_r, mae_r, bars_held)``; ``(None, None, n)`` when the risk
    unit is non-positive (degenerate stop) or the window is empty.
    """
    from core.position_manager import risk_unit
    entry = float(entry_price)
    risk = risk_unit(side, entry, float(initial_stop))
    n = int(len(bars))
    if risk <= 0 or n == 0:
        return None, None, n
    hi = float(bars["high"].max())
    lo = float(bars["low"].min())
    if side == "long":
        mfe_r = (hi - entry) / risk
        mae_r = (lo - entry) / risk
    else:
        mfe_r = (entry - lo) / risk
        mae_r = (entry - hi) / risk
    return mfe_r, mae_r, n


def summarize(rows: list[dict]) -> dict:
    """Aggregate classified trades into per-bucket stats.

    ``rows`` items carry ``bucket``/``r``/``mfe_r``/``mae_r``/``bars``. Returns
    ``{bucket: {n, pct, avg_r, avg_mfe, avg_mae, avg_bars}}`` plus ``__n__`` (total)
    and ``__loss_doa_pct__`` (DOA as a share of losers — the headline number). Empty
    buckets are omitted. Percentages are of the total scored count.
    """
    by = defaultdict(list)
    for row in rows:
        by[row["bucket"]].append(row)
    n = len(rows)
    out: dict = {"__n__": n}
    losers = sum(1 for row in rows if row["bucket"] in ("DOA", "stalled", "gave_back"))
    out["__loss_doa_pct__"] = (100.0 * len(by["DOA"]) / losers) if losers else 0.0
    if not n:
        return out

    def _avg(items, key):
        vals = [it[key] for it in items if it[key] is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    for bucket in BUCKETS:
        items = by.get(bucket)
        if not items:
            continue
        out[bucket] = {
            "n": len(items),
            "pct": 100.0 * len(items) / n,
            "avg_r": _avg(items, "r"),
            "avg_mfe": _avg(items, "mfe_r"),
            "avg_mae": _avg(items, "mae_r"),
            "avg_bars": _avg(items, "bars"),
        }
    return out


# ── live side (real fills off the positions table + parquet cache) ───────────────

def _cfg_commission_r() -> float:
    import yaml
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    return float((c.get("execution", {}) or {}).get("commission_r", 0.005))


def _realized_r(pos, risk: float, commission_r: float, partials) -> float:
    """Size-weighted realized R for a closed position (matches reconcile_fills):
    each scaled-out leg at its fraction + the remainder at the final exit, one
    commission per position. No partials → R of the single exit."""
    entry, side = float(pos.entry_price), pos.side
    legs = [(float(pp.exit_price), float(pp.fraction)) for pp in partials]
    remaining = max(0.0, 1.0 - sum(f for _px, f in legs))
    legs.append((float(pos.exit_price), remaining))
    r = sum(f * (((px - entry) if side == "long" else (entry - px)) / risk) for px, f in legs)
    return r - commission_r


def live_rows(commission_r: float) -> tuple[list[dict], dict]:
    """Classify every closed real position. Returns (rows, skipped) where skipped
    counts ``no_stop`` / ``bad_risk`` / ``cache_miss`` / ``corrupt`` positions that
    can't be graded, plus a ``corrupt_detail`` list for the hygiene warning."""
    from core import position_manager as pm
    from core.position_manager import risk_unit
    from persistence import cache

    closed = [p for p in pm.list_all() if not p.is_open]
    rows: list[dict] = []
    skipped = {"no_stop": 0, "bad_risk": 0, "cache_miss": 0, "corrupt_detail": []}
    for p in closed:
        stop = p.initial_stop if p.initial_stop is not None else p.stop_price
        if stop is None:
            skipped["no_stop"] += 1
            continue
        risk = risk_unit(p.side, float(p.entry_price), float(stop))
        if risk <= 0:
            skipped["bad_risk"] += 1
            skipped["corrupt_detail"].append(
                f"{p.ticker} #{p.id}: {p.side} entry {float(p.entry_price):g} / stop {float(stop):g}")
            continue
        try:
            bars = cache.load(p.ticker, p.entry_date.isoformat(), p.exit_date.isoformat())
        except FileNotFoundError:
            skipped["cache_miss"] += 1
            continue
        partials = pm.get_partials(p.id)
        r = _realized_r(p, risk, commission_r, partials)
        mfe_r, mae_r, bars_held = excursions(bars, p.entry_price, stop, p.side)
        rows.append({"ticker": p.ticker, "side": p.side, "r": r,
                     "mfe_r": mfe_r, "mae_r": mae_r, "bars": bars_held,
                     "bucket": classify(mfe_r, r), "exit_date": p.exit_date})
    return rows, skipped


# ── backtest side (reference run; mfe_r/mae_r already stored) ─────────────────────

def backtest_rows(bt_run_id):
    """Classified trades from the backtest reference run, plus the ref row. Uses the
    stored mfe_r/mae_r — no cache needed. Returns (rows, ref) or ([], None)."""
    from persistence.db_conn import connect
    from backtest.db import reference_run
    conn = connect()
    try:
        cur = conn.cursor(dictionary=True)
        ref = reference_run(cur, bt_run_id)
        if ref is None:
            return [], None
        cur.execute(
            "SELECT r_multiple, mfe_r, mae_r, bars_held FROM backtest_trades "
            "WHERE run_id = %s AND r_multiple IS NOT NULL", (ref["id"],))
        rows = []
        for t in cur.fetchall():
            r = float(t["r_multiple"])
            mfe = float(t["mfe_r"]) if t["mfe_r"] is not None else None
            rows.append({"r": r, "mfe_r": mfe,
                         "mae_r": float(t["mae_r"]) if t["mae_r"] is not None else None,
                         "bars": t["bars_held"], "bucket": classify(mfe, r)})
        return rows, ref
    finally:
        conn.close()


# ── report ───────────────────────────────────────────────────────────────────────

def _print_taxonomy(title: str, stats: dict) -> None:
    n = stats["__n__"]
    print(f"\n  {title}  ·  {n} scored")
    if not n:
        print("    (nothing to score yet)")
        return
    print(f"    {'bucket':<11} {'n':>5} {'share':>7} {'avg R':>8} {'avg MFE':>8} "
          f"{'avg MAE':>8} {'avg bars':>8}")
    for bucket in BUCKETS:
        s = stats.get(bucket)
        if not s:
            continue
        print(f"    {bucket:<11} {s['n']:>5} {s['pct']:>6.1f}% {s['avg_r']:>+8.3f} "
              f"{s['avg_mfe']:>8.2f} {s['avg_mae']:>8.2f} {s['avg_bars']:>8.1f}")
    print(f"    → DOA = {stats['__loss_doa_pct__']:.1f}% of losers "
          f"(the dead-on-arrival false-positive rate)")


def _print_offenders(rows: list[dict], min_ticker: int) -> None:
    by = defaultdict(list)
    for row in rows:
        by[row["ticker"]].append(row)
    stats = []
    for tk, rr in by.items():
        if len(rr) < min_ticker:
            continue
        wins = sum(1 for x in rr if x["r"] > 0)
        stats.append((tk, len(rr), 100.0 * wins / len(rr), sum(x["r"] for x in rr)))
    if not stats:
        print(f"\n  Per-ticker offenders: none with >= {min_ticker} closed trades yet.")
        return
    stats.sort(key=lambda x: x[3])
    print(f"\n  Per-ticker (>= {min_ticker} closed, worst realized R first — universe-pruning candidates):")
    print(f"    {'ticker':<10} {'n':>4} {'win%':>6} {'sum R':>8}")
    for tk, cnt, wr, sr in stats:
        print(f"    {tk:<10} {cnt:>4} {wr:>5.1f}% {sr:>+8.2f}")


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Live false-positive (DOA) taxonomy report")
    ap.add_argument("--bt-run-id", type=int, default=None,
                    help="backtest_runs.id for the reference taxonomy (default: latest scoring-OFF run).")
    ap.add_argument("--min-ticker", type=int, default=3,
                    help="min closed trades for a ticker to appear in the offender list (default 3).")
    ap.add_argument("--commission-r", type=float, default=None,
                    help="per-trade commission in R (default: execution.commission_r from filters.yaml).")
    args = ap.parse_args()

    commission_r = args.commission_r if args.commission_r is not None else _cfg_commission_r()

    try:
        bt, ref = backtest_rows(args.bt_run_id)
    except Exception as exc:
        print(f"  ✗ DB/backtest read failed ({exc}). Set DB_* in config/secrets.env."); return

    print("=" * 78)
    print("  FALSE-POSITIVE TAXONOMY  ·  live fills vs backtest reference")
    print("=" * 78)
    if ref is not None:
        _print_taxonomy(
            f"BACKTEST reference (run {ref['id']}, {ref['trades_count']} trades)",
            summarize(bt))
    else:
        print("\n  No backtest_runs to reference — journal a backtest first "
              "(python -m backtest.run_backtest).")

    try:
        rows, skipped = live_rows(commission_r)
    except Exception as exc:
        print(f"\n  ✗ Live read failed ({exc})."); return

    _print_taxonomy("LIVE real fills", summarize(rows))
    note = [f"{v} {k}" for k, v in (("no-stop", skipped["no_stop"]),
                                    ("bad-geometry", skipped["bad_risk"]),
                                    ("cache-miss", skipped["cache_miss"])) if v]
    if note:
        print(f"    unscored: {', '.join(note)}")
    if skipped["corrupt_detail"]:
        print("    ⚠ corrupt stop geometry (fix in the journal — a long stop must be BELOW entry):")
        for d in skipped["corrupt_detail"]:
            print(f"        {d}")
    if len(rows) < 30:
        print(f"\n  ⚠ Only {len(rows)} scored live fill(s) — too few to conclude "
              f"(Phase 5 needs >= 30). The backtest column is the baseline until then.")
    _print_offenders(rows, args.min_ticker)
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
