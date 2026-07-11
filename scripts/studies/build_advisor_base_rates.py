"""Precompute the advisor's historical base-rate table → data/advisor_base_rates.json.

Aggregates resolved ``backtest_trades`` into win-rate + mean R + count per setup
cell (signal_type, then × regime, then × trend). Aggregates only — no per-trade
rows — so the table the live advisor reads cannot leak the outcome of the trade
under review. Regenerate after any backtest that repopulates backtest_trades.

Usage:
    python scripts/studies/build_advisor_base_rates.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")  # DB_* for the connection
except ImportError:
    pass

from core.paths import DATA_DIR  # noqa: E402


def _cell(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {}
    return {"n": n, "win_rate": sum(1 for r in rs if r > 0) / n, "avg_r": sum(rs) / n}


def main() -> None:
    from persistence.db_conn import connect

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT signal_type, market_regime, ticker_trend, r_multiple
            FROM backtest_trades
            WHERE r_multiple IS NOT NULL AND signal_type IS NOT NULL
            """
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    buckets: dict[str, list[float]] = defaultdict(list)
    for signal_type, regime, trend, r in rows:
        st = str(signal_type or "").lower()
        rg = str(regime or "").upper()
        tr = str(trend or "").upper()
        rv = float(r)
        buckets["__all__"].append(rv)
        if not st:
            continue
        buckets[st].append(rv)
        if rg:
            buckets[f"{st}|{rg}"].append(rv)
            if tr:
                buckets[f"{st}|{rg}|{tr}"].append(rv)

    table = {k: _cell(v) for k, v in buckets.items() if v}
    out = DATA_DIR / "advisor_base_rates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, sort_keys=True)

    print(f"  wrote {len(table)} cells -> {out}")
    g = table.get("__all__", {})
    if g:
        print(f"  overall: {g['win_rate']:.0%} win, {g['avg_r']:+.3f}R avg, n={g['n']}")


if __name__ == "__main__":
    main()
