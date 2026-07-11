"""Precompute the advisor's own live calibration → data/advisor_calibration.json.

For every fired live entry that carries a journaled verdict (scan_results.
advisor_note), replay the signal forward the same way reconcile_live does, then
bucket the realized R by verdict: how often 'agree' actually won, how often
'disagree' actually lost, and what those trades really averaged. Aggregates only
— look-ahead safe. Fail-open downstream: until enough live verdicts mature this
writes a thin/empty table and the advisor adds no calibration line.

Reuses reconcile_live's replay + config so the R here matches the live meter.

Usage:
    python scripts/studies/build_advisor_calibration.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src"), str(_ROOT / "scripts" / "live")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass

from core.paths import DATA_DIR  # noqa: E402


def _verdict_of(note: str) -> str | None:
    """Parse the verdict label from a rendered advisor note prefix.

    format_note emits e.g. '❌ Disagree · 85% — …'. Check 'disagree' before
    'agree' ('agree' is a substring) and only scan the label prefix."""
    low = str(note or "").lower()[:24]
    if "disagree" in low:
        return "disagree"
    if "agree" in low:
        return "agree"
    if "flag" in low:
        return "flag"
    return None


def _cell(rs: list[float], label: str) -> dict:
    n = len(rs)
    if not n:
        return {}
    if label == "agree":
        correct = sum(1 for r in rs if r > 0) / n
    elif label == "disagree":
        correct = sum(1 for r in rs if r <= 0) / n
    else:  # flag is not a directional call — track size only
        correct = 0.0
    return {"n": n, "correct": correct, "avg_r": sum(rs) / n}


def main() -> None:
    import pandas as pd
    import reconcile_live as rl
    from backtest.backtester import (
        adjust_target_for_slippage, apply_stop_fill, apply_stop_fill_short,
        apply_target_fill, apply_target_fill_short,
    )
    from persistence.cache import load as cache_load
    from persistence.db_conn import connect

    cfg = rl._cfg()
    max_hold = cfg["max_hold_days"]
    max_hold_mode = cfg["max_hold_mode"].replace("-", "_")

    conn = connect()
    try:
        cur = conn.cursor(dictionary=True)
        has_tier = rl._has_tier_column(cur)
        tier_filter = " AND (sr.tier IS NULL OR sr.tier = 'LIVE') " if has_tier else " "
        cur.execute(
            "SELECT sr.ticker, sr.signal_kind, sr.close, sr.atr, sr.stop_price, "
            "sr.target_price, sr.signal_type, sr.advisor_note, "
            "r.created_at, r.market_regime "
            "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
            "WHERE sr.passed = 1 AND sr.signal_kind IN ('entry_long','entry_short') "
            "AND sr.advisor_note IS NOT NULL AND sr.advisor_note <> ''"
            + tier_filter
        )
        sigs = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    buckets: dict[str, list[float]] = {}
    pending = errors = 0
    for s in sigs:
        verdict = _verdict_of(s["advisor_note"])
        if verdict is None:
            continue
        try:
            df = cache_load(s["ticker"])
        except Exception:
            errors += 1
            continue
        entry_idx = int(df.index.searchsorted(pd.Timestamp(s["created_at"]).normalize(),
                                              side="right"))  # T+1
        if entry_idx >= len(df):
            pending += 1
            continue
        entry = float(df.iloc[entry_idx]["open"])
        is_short = s["signal_kind"] == "entry_short"
        close_d = float(s["close"]) if s["close"] is not None else entry
        atr = float(s["atr"]) if s["atr"] is not None else 0.0

        if s["stop_price"] is not None and s["target_price"] is not None:
            stop, target = float(s["stop_price"]), float(s["target_price"])
        elif is_short:
            stop = close_d + atr * cfg["atr_mult"]
            target = close_d - (stop - close_d) * cfg["min_rr"]
        else:
            stop = close_d - atr * cfg["atr_mult"]
            target = close_d + (close_d - stop) * cfg["min_rr"]

        slip = cfg["entry_slippage_pct"]
        if slip:
            entry *= (1.0 - slip) if is_short else (1.0 + slip)
            target = adjust_target_for_slippage(
                entry, stop, target, cfg["min_rr"],
                direction="short" if is_short else "long")

        risk = (stop - entry) if is_short else (entry - stop)
        if risk <= 0:
            errors += 1
            continue

        exit_price, _reason = rl._replay(
            df, entry_idx, entry, stop, target, is_short, max_hold, max_hold_mode,
            apply_stop_fill, apply_target_fill, apply_stop_fill_short, apply_target_fill_short)
        if exit_price is None:
            pending += 1
            continue
        r = ((entry - exit_price) / risk) if is_short else ((exit_price - entry) / risk)
        r -= cfg["commission_r"]
        buckets.setdefault(verdict, []).append(r)

    by_verdict = {k: _cell(v, k) for k, v in buckets.items() if v}
    total = sum(len(v) for v in buckets.values())
    table = {"n": total, "by_verdict": by_verdict}

    out = DATA_DIR / "advisor_calibration.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, sort_keys=True)

    print(f"  wrote calibration over {total} resolved advisor calls -> {out}")
    print(f"  (pending/too-recent: {pending}, errors/skipped: {errors})")
    for k, c in sorted(by_verdict.items()):
        print(f"    {k}: {c}")


if __name__ == "__main__":
    main()
