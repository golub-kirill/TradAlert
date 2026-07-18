"""Frame-contamination meter — fired live entries the backtest frame would block.

Until 2026-07-17 the live scanner ran on the wall clock while the backtest pins
``engine._today`` to the bar. The earnings gate is forward-only, so an entry
whose earnings date fell inside ``[bar_date, bar_date + buffer]`` could fire
live (already "past" on the wall clock) while the backtest blocks it — the two
paths screened different populations. This meter replays the bar-frame earnings
check over the journaled entry alerts and counts the divergent fires, so the
pre-fix Phase-5 segment can be priced and, if needed, segregated.

Read-only: SELECTs scan_results/scan_runs, reads the earnings-date caches
(data/earnings_history + data/fundamentals sections), writes nothing.

Usage:
    python scripts/live/frame_contamination.py [--buffer N]

Buffer defaults to filters.yaml events.earnings_buffer_days.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / "config" / "secrets.env")

from core.freshness import exchange_for, last_completed_session  # noqa: E402

_EH_DIR = _ROOT / "data" / "earnings_history"
_FUND_DIR = _ROOT / "data" / "fundamentals"


def _buffer_days() -> int:
    import yaml
    cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    return int((cfg.get("events") or {}).get("earnings_buffer_days", 5))


def _earnings_dates(ticker: str) -> list[date]:
    """Union of both caches' known earnings dates (raw read, no staleness —
    historical dates don't move and future misses are reported as no-data)."""
    out: set[date] = set()
    p = _EH_DIR / f"{ticker.upper()}.json"
    if p.exists():
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            out |= {date.fromisoformat(s) for s in payload.get("dates", [])}
        except (json.JSONDecodeError, ValueError, KeyError, OSError):
            pass
    p = _FUND_DIR / f"{ticker.upper()}.json"
    if p.exists():
        try:
            sect = (json.loads(p.read_text(encoding="utf-8")) or {}).get("earnings_history") or {}
            out |= {date.fromisoformat(s) for s in sect.get("dates", [])}
        except (json.JSONDecodeError, ValueError, KeyError, OSError):
            pass
    return sorted(out)


@dataclass
class Hit:
    run_id: int
    ticker: str
    kind: str
    tier: str
    scan_wall: date        # live _today at scan time (wall clock)
    bar: date              # the bar the signal was computed on
    earnings: date         # the date inside the frame buffer
    mechanism: str         # why live let it through


def _fetch_entries():
    from persistence.db_conn import connect
    sql = """
        SELECT r.run_id, r.ticker, r.signal_kind, r.tier, s.created_at
        FROM scan_results r JOIN scan_runs s ON s.id = r.run_id
        WHERE r.signal_kind IN ('entry_long', 'entry_short')
        ORDER BY s.created_at, r.ticker
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Bar-frame vs wall-clock earnings-gate replay")
    ap.add_argument("--buffer", type=int, default=None,
                    help="earnings buffer days (default: filters.yaml)")
    args = ap.parse_args()
    buffer = args.buffer if args.buffer is not None else _buffer_days()

    rows = _fetch_entries()
    if not rows:
        print("no journaled entry alerts found")
        return 0

    hits: list[Hit] = []
    no_data: set[str] = set()
    same_frame = 0
    for run_id, ticker, kind, tier, created_at in rows:
        # MySQL TIMESTAMP comes back naive in the server's (local) timezone.
        created_local = created_at.astimezone()
        wall = created_local.date()
        bar = last_completed_session(created_local, exchange_for(ticker))
        if bar == wall:
            same_frame += 1  # scan ran post-close same day: frames identical
        dates = _earnings_dates(ticker)
        if not dates:
            no_data.add(ticker)
            continue
        for e in dates:
            if bar <= e <= bar + timedelta(days=buffer):
                mech = ("past-on-wall-clock" if e < wall
                        else "vendor-miss (live should also have blocked)")
                hits.append(Hit(run_id, ticker, kind, tier, wall, bar, e, mech))
                break

    total = len(rows)
    first, last = rows[0][4].date(), rows[-1][4].date()
    print(f"\n  FRAME CONTAMINATION — journaled entry alerts {first} → {last}")
    print(f"  buffer = {buffer}d (events.earnings_buffer_days)")
    print("  " + "─" * 74)
    for h in hits:
        print(f"  run {h.run_id:>4}  {h.ticker:<10} {h.kind:<11} {h.tier:<12} "
              f"bar {h.bar}  earnings {h.earnings}  [{h.mechanism}]")
    if not hits:
        print("  (no divergent fires)")
    print("  " + "─" * 74)
    print(f"  entries journaled : {total}")
    print(f"  same-frame scans  : {same_frame} (bar == wall clock; divergence impossible)")
    print(f"  no earnings data  : {len(no_data)} ticker(s)"
          + (f" — {', '.join(sorted(no_data))}" if no_data else ""))
    print(f"  DIVERGENT FIRES   : {len(hits)} / {total} "
          f"({100.0 * len(hits) / total:.1f}%) — fired live, bar-frame would block")
    print("  NOTE: fires after the 2026-07-17 frame pin can no longer diverge; rows"
          "\n        before it are the pre-fix Phase-5 segment this meter prices.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
