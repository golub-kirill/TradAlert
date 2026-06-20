#!/usr/bin/env python3
"""
Paired A/B for the NAAIM purge: positioning = COT+NAAIM (current) vs COT-only.

ONE load_universe() call → identical in-memory data both legs (pinned snapshot),
so the delta is the ISOLATED NAAIM contribution (jitter-free), the honest cost of
dropping NAAIM — the same measure used for the AAII/sentiment purge. Toggles
``settings.behavioral.use_naaim``. Leg A (true) == the +113.58R headline; Leg B
(false) = COT-only positioning.

    .venv/Scripts/python.exe scripts/naaim_ab.py [--snapshot data/snapshot_2026-06-10]
"""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe       # noqa: E402
from backtest.stats import compute_stats        # noqa: E402

from paired_ab import _run  # noqa: E402


def _line(label, trades):
    st = compute_stats(trades)
    ec = build_curve(trades)
    return (f"  {label:<18} {st.trades_count:>6}  {st.win_rate:>6.1%}  "
            f"{st.expectancy_r:>+7.3f}  {ec.total_r:>+8.2f}  {ec.sharpe:>6.2f}  "
            f"{ec.sortino:>7.2f}  {ec.max_dd:>6.2f}")


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    base = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    settings = yaml.safe_load((_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", []) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral",
        start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)

    s_on = settings                       # use_naaim true (current) → COT+NAAIM
    s_off = copy.deepcopy(settings)
    s_off.setdefault("behavioral", {})["use_naaim"] = False   # COT-only

    trades_on = _run(uni, base, s_on, breakeven_trigger_r=1.0)
    trades_off = _run(uni, base, s_off, breakeven_trigger_r=1.0)

    print("\n  NAAIM A/B — positioning COT+NAAIM vs COT-only (breakeven 1.0R, same snapshot)")
    print("  " + "─" * 80)
    print(f"  {'config':<18} {'trades':>6}  {'WR':>6}  {'E[R]':>7}  "
          f"{'totalR':>8}  {'Sharpe':>6}  {'Sortino':>7}  {'maxDD':>6}")
    print(_line("COT+NAAIM (base)", trades_on))
    print(_line("COT-only", trades_off))

    ec_on, ec_off = build_curve(trades_on), build_curve(trades_off)
    st_on, st_off = compute_stats(trades_on), compute_stats(trades_off)
    print("  " + "─" * 80)
    print(f"  NAAIM-drop effect (COT-only − base): {ec_off.total_r - ec_on.total_r:+.2f}R · "
          f"{ec_off.sharpe - ec_on.sharpe:+.3f} Sharpe · "
          f"{ec_off.sortino - ec_on.sortino:+.3f} Sortino · "
          f"{ec_off.max_dd - ec_on.max_dd:+.2f}R maxDD · "
          f"{st_off.trades_count - st_on.trades_count:+d} trades")
    print("  " + "─" * 80)
    print("  Byte-identical check: 'COT+NAAIM (base)' must reproduce +113.58R / 1635t / 0.57.")


if __name__ == "__main__":
    main()
