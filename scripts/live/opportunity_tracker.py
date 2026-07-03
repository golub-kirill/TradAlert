#!/usr/bin/env python3
"""
Opportunity-cost shadow tracker — what did the scanner's gates cost (or save)?

A read-only postmortem over the live journal. For every name the scanner
*passed on* (recorded in `scan_results` / `scan_runs`) it computes the realized
**market-adjusted forward return** and turns "I skipped a winner" into an honest
two-sided number per rejecting gate: avoided losers vs missed winners.

A row is "passed on" when:
  • `passed = 0`                                      → scan-blocked (gate = its `reason`), OR
  • `passed = 1 AND signal_kind IN ('none') / NULL`   → passed scan but nothing fired
                                                        (gate = its `reason`, which now holds
                                                        the signal-stage reason), OR
  • `passed = 1 AND declined = 1`                     → a FIRED entry the owner skipped via the
                                                        Telegram 🚫 Skip button (gate = 'declined').

For each such (ticker, scan_date, gate) the forward return from the bar on/after
scan_date is market-adjusted vs SPY over the identical span (same `.asof` approach
as `core.pead.car_event`), then classified per gate:
  • > +win  → missed_winner   (the gate cost you)
  • < -lose → avoided_loser   (the gate saved you)
  • else    → neutral

Overlap control: the same name is often blocked on many consecutive days, so the
forward windows overlap heavily. Headline stats dedupe to one observation per
(ticker, gate, year-month) — the earliest scan_date that month. The raw passed-on
count and the deduped/matured count are both reported so the truncation is visible.

    python scripts/live/opportunity_tracker.py
    python scripts/live/opportunity_tracker.py --days-back 90 --win 0.05 --lose 0.05

Requires DB_* in config/secrets.env (same as the live scanner) and the price cache.
Read-only: never touches the engine/backtester/signal code and never writes the DB.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
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


# ── pure helpers (import-safe; unit-tested; no DB / no network) ──────────────

def forward_returns(close: np.ndarray, dates, spy_close, i0: int,
                    horizons=(5, 21)) -> dict:
    """Market-adjusted forward returns from index ``i0`` (the bar on/after scan_date).

    For each horizon ``h``::

        mkt_adj_h = (close[i0+h]/close[i0] - 1)
                    - (spy.asof(dates[i0+h])/spy.asof(dates[i0]) - 1)

    NaN when ``i0+h`` is past the end of the series or any of the four prices is
    non-finite or <= 0 (mirrors ``core.pead.car_event``'s finiteness guards and
    ``.asof`` usage).

    Also returns ``mdd21`` — the worst close-to-close drawdown over the 21-bar
    window from ``i0`` (the avoided downside), as a non-positive fraction; NaN
    when the full 21-bar window does not exist or contains a bad price.

    Returns a dict ``{"fwd5": ..., "fwd21": ..., "mdd21": ...}`` keyed by
    ``f"fwd{h}"`` for each horizon plus ``mdd21``.
    """
    n = len(close)
    out: dict[str, float] = {}

    c0 = close[i0] if 0 <= i0 < n else float("nan")
    spy0 = spy_close.asof(dates[i0]) if 0 <= i0 < n else float("nan")
    base_ok = (0 <= i0 < n and np.isfinite(c0) and c0 > 0
               and np.isfinite(spy0) and spy0 > 0)

    for h in horizons:
        j = i0 + h
        if not base_ok or j >= n:
            out[f"fwd{h}"] = float("nan")
            continue
        c_h = close[j]
        spy_h = spy_close.asof(dates[j])
        if not (np.isfinite(c_h) and c_h > 0 and np.isfinite(spy_h) and spy_h > 0):
            out[f"fwd{h}"] = float("nan")
            continue
        out[f"fwd{h}"] = (c_h / c0 - 1.0) - (spy_h / spy0 - 1.0)

    # Worst close-to-close drawdown over the 21-bar window (i0 .. i0+21).
    out["mdd21"] = _max_drawdown(close, i0, 21)
    return out


def _max_drawdown(close: np.ndarray, i0: int, window: int) -> float:
    """Worst close-to-close drawdown (a non-positive fraction) over
    ``close[i0 .. i0+window]``. NaN if the full window is missing or any price in
    it is non-finite/<= 0."""
    n = len(close)
    j_end = i0 + window
    if i0 < 0 or j_end >= n:
        return float("nan")
    seg = close[i0:j_end + 1]
    if not np.all(np.isfinite(seg)) or np.any(seg <= 0):
        return float("nan")
    peak = seg[0]
    worst = 0.0
    for c in seg:
        if c > peak:
            peak = c
        dd = c / peak - 1.0
        if dd < worst:
            worst = dd
    return float(worst)


def classify(mkt_adj_fwd21: float, *, win: float = 0.05, lose: float = 0.05) -> str:
    """Two-sided label for a market-adjusted forward return.

    ``"missed_winner"`` if ``> +win``, ``"avoided_loser"`` if ``< -lose``, else
    ``"neutral"``. ``win``/``lose`` are market-adjusted return thresholds
    (default ±5%). NaN classifies as ``"neutral"``.
    """
    if not np.isfinite(mkt_adj_fwd21):
        return "neutral"
    if mkt_adj_fwd21 > win:
        return "missed_winner"
    if mkt_adj_fwd21 < -lose:
        return "avoided_loser"
    return "neutral"


def aggregate(observations: list[dict]) -> dict:
    """Per-gate rollup over observations.

    Each obs has ``gate``, ``fwd5``, ``fwd21``, ``cls``. NaN ``fwd21`` rows are
    dropped from the numeric stats (but the gate still appears if it has any
    valid row). Per gate returns::

        {"n", "median_fwd21", "mean_fwd21", "pct_missed_winner",
         "pct_avoided_loser", "net"}

    where ``net`` is the mean market-adjusted fwd21 — negative ⇒ the gate avoided
    losers, positive ⇒ it cost you. Also returns an ``"__ALL__"`` overall rollup.
    """
    by_gate: dict[str, list[dict]] = defaultdict(list)
    for o in observations:
        by_gate[o["gate"]].append(o)

    def _roll(rows: list[dict]) -> dict:
        fwd = [r["fwd21"] for r in rows if np.isfinite(r.get("fwd21", float("nan")))]
        n = len(fwd)
        miss = sum(1 for r in rows if r.get("cls") == "missed_winner")
        avoid = sum(1 for r in rows if r.get("cls") == "avoided_loser")
        denom = len(rows)
        return {
            "n": n,
            "median_fwd21": float(np.median(fwd)) if n else float("nan"),
            "mean_fwd21": float(np.mean(fwd)) if n else float("nan"),
            "pct_missed_winner": (100.0 * miss / denom) if denom else 0.0,
            "pct_avoided_loser": (100.0 * avoid / denom) if denom else 0.0,
            "net": float(np.mean(fwd)) if n else float("nan"),
        }

    result = {gate: _roll(rows) for gate, rows in by_gate.items()}
    result["__ALL__"] = _roll(observations)
    return result


# ── DB + price I/O (main; not import-safe) ──────────────────────────────────

def _fetch_passed_on(conn, days_back: int | None) -> list[dict]:
    """Passed-on rows: (ticker, scan_date, gate). See module docstring for the
    definition. Ordered by scan_date, ticker so the monthly dedupe keeps the
    earliest scan_date deterministically."""
    cur = conn.cursor(dictionary=True)
    where_days = ""
    params: tuple = ()
    if days_back is not None:
        where_days = " AND r.created_at >= (NOW() - INTERVAL %s DAY) "
        params = (int(days_back),)
    cur.execute(
        "SELECT sr.ticker, DATE(r.created_at) AS scan_date, sr.reason AS gate, "
        "       sr.declined AS declined "
        "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
        "WHERE (sr.passed = 0 OR (sr.passed = 1 AND (sr.signal_kind IS NULL "
        "       OR sr.signal_kind = 'none')) "
        "       OR (sr.passed = 1 AND sr.declined = 1)) "
        + where_days +
        "ORDER BY scan_date, sr.ticker",
        params,
    )
    rows = cur.fetchall()
    cur.close()
    out = []
    for row in rows:
        # An owner-declined FIRED signal is a distinct passed-on category (the gate
        # didn't reject it — the owner did) → label it 'declined' so the per-gate
        # rollup separates "I skipped a fire" from "a gate blocked it".
        if row.get("declined"):
            gate = "declined"
        else:
            gate = row["gate"] if row["gate"] else "(unspecified)"
        out.append({"ticker": row["ticker"], "scan_date": row["scan_date"], "gate": gate})
    return out


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Opportunity-cost shadow tracker (read-only postmortem)")
    ap.add_argument("--days-back", type=int, default=None,
                    help="Limit history to the last N days (default: all).")
    ap.add_argument("--prices-dir", default="data/prices",
                    help="Directory of cached {TICKER}.parquet price files.")
    ap.add_argument("--win", type=float, default=0.05,
                    help="Missed-winner threshold (market-adj fwd21 return). Default 0.05.")
    ap.add_argument("--lose", type=float, default=0.05,
                    help="Avoided-loser threshold (market-adj fwd21 return). Default 0.05.")
    args = ap.parse_args()

    import pandas as pd
    from persistence.db_conn import connect

    prices_dir = (Path(args.prices_dir) if Path(args.prices_dir).is_absolute()
                  else _ROOT / args.prices_dir)

    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env.")
        return
    try:
        passed_on = _fetch_passed_on(conn, args.days_back)
    finally:
        conn.close()

    if not passed_on:
        print("  No passed-on history yet — run the daily scan to accumulate "
              "(python main.py).")
        return

    # SPY market benchmark — loaded once, as a close Series for .asof lookups.
    spy_path = prices_dir / "SPY.parquet"
    if not spy_path.exists():
        print(f"  ✗ SPY benchmark missing at {spy_path} — cannot market-adjust. "
              f"Populate the price cache first.")
        return
    spy_df = pd.read_parquet(spy_path)
    spy_df.index = pd.to_datetime(spy_df.index)
    spy_close = spy_df["close"].sort_index()

    # Per-ticker price cache so each parquet is read once across many scan dates.
    price_cache: dict[str, pd.DataFrame | None] = {}

    def _load_prices(ticker: str):
        if ticker not in price_cache:
            p = prices_dir / f"{ticker}.parquet"
            if not p.exists():
                price_cache[ticker] = None
            else:
                try:
                    d = pd.read_parquet(p)
                    d.index = pd.to_datetime(d.index)
                    price_cache[ticker] = d.sort_index()
                except Exception:
                    price_cache[ticker] = None
        return price_cache[ticker]

    raw_count = len(passed_on)
    observations: list[dict] = []      # deduped (one per ticker,gate,year-month), matured
    missing_price = set()
    not_matured = 0
    bad = 0
    seen_keys: set[tuple] = set()      # (ticker, gate, year-month) → earliest kept

    for rec in passed_on:
        ticker = rec["ticker"]
        gate = rec["gate"]
        scan_date = rec["scan_date"]
        try:
            d = pd.Timestamp(scan_date)
        except Exception:
            bad += 1
            continue

        # Monthly dedupe — rows are scan_date-ascending, so the first row seen for
        # a (ticker, gate, year-month) is the earliest; later ones are dropped.
        key = (ticker, gate, d.year, d.month)
        if key in seen_keys:
            continue

        df = _load_prices(ticker)
        if df is None:
            missing_price.add(ticker)
            continue
        try:
            dates = df.index
            close = df["close"].to_numpy(dtype=float)
            # First bar on/after scan_date (the shadow "entry" reference bar).
            i0 = int(dates.searchsorted(d.normalize(), side="left"))
            if i0 >= len(close):
                not_matured += 1
                continue
            # Require the full +21d window to exist, else the observation hasn't matured.
            if i0 + 21 >= len(close):
                not_matured += 1
                continue
            fr = forward_returns(close, dates, spy_close, i0, horizons=(5, 21))
        except Exception:
            bad += 1
            continue

        cls = classify(fr["fwd21"], win=args.win, lose=args.lose)
        observations.append({
            "ticker": ticker,
            "gate": gate,
            "scan_date": d.date(),
            "fwd5": fr["fwd5"],
            "fwd21": fr["fwd21"],
            "mdd21": fr["mdd21"],
            "cls": cls,
        })
        seen_keys.add(key)

    if not observations:
        print(f"\n  Passed-on rows: {raw_count}   ·   matured (21d): 0")
        details = []
        if not_matured:
            details.append(f"{not_matured} too recent (no +21d window yet)")
        if missing_price:
            details.append(f"{len(missing_price)} ticker(s) missing price cache")
        if bad:
            details.append(f"{bad} skipped (bad row/price)")
        if details:
            print("  " + "   ".join(details))
        print("\n  ⚠ Nothing matured yet — keep the scanner running and rerun once "
              "passed-on names age ~21 trading days.\n")
        return

    agg = aggregate(observations)

    dates_seen = [o["scan_date"] for o in observations]
    print(f"\n  Opportunity-cost shadow tracker  ·  passed-on rows {raw_count}  ·  "
          f"deduped/matured {len(observations)}  ·  "
          f"{min(dates_seen)} → {max(dates_seen)}  ·  win>+{args.win:.0%} lose<-{args.lose:.0%}")
    extra = []
    if not_matured:
        extra.append(f"{not_matured} not matured")
    if missing_price:
        extra.append(f"{len(missing_price)} missing price")
    if bad:
        extra.append(f"{bad} bad/skipped")
    if extra:
        print("  (" + ", ".join(extra) + ")")

    print("\n" + "=" * 92)
    print(f"  {'Gate':<34} {'n':>4} {'med fwd21':>10} {'%missed':>8} "
          f"{'%avoided':>9} {'net-mean':>9}  read")
    print("  " + "-" * 88)

    gate_keys = [g for g in agg if g != "__ALL__"]
    gate_keys.sort(key=lambda g: (-agg[g]["n"], g))
    for g in gate_keys:
        a = agg[g]
        net = a["net"]
        if not np.isfinite(net):
            read = ""
        elif net < 0:
            read = "avoided losers"
        elif net > 0:
            read = "cost you"
        else:
            read = "flat"
        label = g if len(g) <= 34 else g[:31] + "..."
        med = a["median_fwd21"]
        med_s = f"{med:>+10.2%}" if np.isfinite(med) else f"{'n/a':>10}"
        net_s = f"{net:>+9.2%}" if np.isfinite(net) else f"{'n/a':>9}"
        print(f"  {label:<34} {a['n']:>4} {med_s} {a['pct_missed_winner']:>7.0f}% "
              f"{a['pct_avoided_loser']:>8.0f}% {net_s}  {read}")

    print("  " + "-" * 88)
    allr = agg["__ALL__"]
    net = allr["net"]
    if not np.isfinite(net):
        verdict = "—"
    elif net < 0:
        verdict = (f"gates net-AVOIDED losers ({net:+.2%} mean mkt-adj fwd21) — "
                   f"the passed-on book underperformed SPY")
    elif net > 0:
        verdict = (f"gates net-COST you ({net:+.2%} mean mkt-adj fwd21) — "
                   f"the passed-on book beat SPY")
    else:
        verdict = "gates net-flat vs SPY"
    net_s = f"{net:>+9.2%}" if np.isfinite(net) else f"{'n/a':>9}"
    med = allr["median_fwd21"]
    med_s = f"{med:>+10.2%}" if np.isfinite(med) else f"{'n/a':>10}"
    print(f"  {'ALL':<34} {allr['n']:>4} {med_s} {allr['pct_missed_winner']:>7.0f}% "
          f"{allr['pct_avoided_loser']:>8.0f}% {net_s}")
    print("=" * 92)
    print(f"\n  Two-sided read: {verdict}.\n"
          f"  (negative net-mean ⇒ the gate avoided losers; positive ⇒ it cost you a winner.)\n")


if __name__ == "__main__":
    main()
