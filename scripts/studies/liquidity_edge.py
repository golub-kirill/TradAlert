#!/usr/bin/env python3
"""
Liquidity / market-efficiency edge test (v1) — see docs/backtest_out/liquidity_edge_prereg.md.

Buckets every long momentum trade in a backtest run by its point-in-time liquidity
(ADV20 = mean close×volume over the 20 bars STRICTLY BEFORE entry, from the price
cache), ranked within its entry-year cohort into deciles, and reports E[R]/trade by
decile — pooled and across 3 walk-forward eras — plus each decile's CA / resource
composition so the "illiquid == Canadian-resource" confound is visible.

This is the v1 (descriptive) pass. A PASS per the pre-registration also requires v2
sector-neutralisation (needs config/sector_map.yaml populated — currently empty).

Read-only. Run from a tree WITH the parquet cache (i.e. main, not a bare worktree):

    python scripts/studies/liquidity_edge.py --run-id 26
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
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass

# Energy + materials ("resource") set — the commodity-beta confound probe. Coarse by
# design (config/sector_map.yaml is empty); refine when a real sector map exists.
RESOURCE = set("""
XLE XLB XOM CVX COP SLB EOG MPC PSX OXY GLD SLV DBC DBA CPER URA UNG
ENB.TO CNQ.TO SU.TO CVE.TO IMO.TO TRP.TO PPL.TO TOU.TO ARX.TO
ABX.TO K.TO AEM.TO FNV.TO WPM.TO NTR.TO TECK-B.TO FM.TO LUN.TO IVN.TO CCO.TO AGI.TO WFG.TO MX.TO
XEG.TO XMA.TO XGD.TO
""".split())


def _era(y: int) -> str:
    if y < 2011:
        return "2000-2010"
    if y < 2018:
        return "2011-2017"
    return "2018-2026"


def _adv20_at(df, entry_date):
    """Median dollar-volume over the 20 bars strictly before entry_date, or None
    when there are fewer than 20 prior bars (point-in-time; no look-ahead)."""
    import pandas as pd
    D = pd.Timestamp(entry_date)
    pos = int(df.index.searchsorted(D))  # first bar >= D; prior bars are [0, pos)
    if pos < 20:
        return None
    window = df.iloc[pos - 20:pos]
    dv = (window["close"] * window["volume"]).mean()
    return float(dv) if dv == dv else None  # NaN guard


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Liquidity-decile momentum expectancy (v1)")
    ap.add_argument("--run-id", type=int, default=26, help="backtest_runs.id (default 26).")
    args = ap.parse_args()

    import pandas as pd
    from persistence.db_conn import connect
    from persistence import cache

    conn = connect()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT ticker, entry_date, r_multiple FROM backtest_trades "
                "WHERE run_id=%s AND direction='long' AND r_multiple IS NOT NULL", (args.run_id,))
    trades = cur.fetchall()
    cur.close(); conn.close()

    # attach point-in-time ADV20 (load each ticker's bars once)
    bars: dict[str, "pd.DataFrame"] = {}
    rows = []
    missing = 0
    for t in trades:
        tk = t["ticker"]
        if tk not in bars:
            try:
                bars[tk] = cache.load(tk)
            except Exception:
                bars[tk] = None
        df = bars[tk]
        adv = _adv20_at(df, t["entry_date"]) if df is not None else None
        if adv is None:
            missing += 1
            continue
        rows.append({"ticker": tk, "year": t["entry_date"].year, "r": float(t["r_multiple"]),
                     "adv": adv, "ca": tk.endswith(".TO"), "res": tk in RESOURCE})
    df = pd.DataFrame(rows)
    n = len(df)
    print(f"\n  Liquidity edge test v1 · run {args.run_id} · {n} trades scored "
          f"({missing} skipped: <20 prior bars / no cache)")

    # decile within entry-year cohort (cohort-relative removes secular liquidity drift)
    df["decile"] = (df.groupby("year")["adv"]
                    .rank(pct=True).mul(10).clip(upper=9.999).astype(int) + 1)

    def _table(sub: "pd.DataFrame", label: str):
        print(f"\n  {label}  (n={len(sub)})")
        print(f"    {'decile':>6} {'n':>5} {'E[R]':>8} {'win%':>7} {'%CA':>6} {'%resource':>10}")
        for d in range(1, 11):
            g = sub[sub["decile"] == d]
            if len(g) == 0:
                continue
            print(f"    {d:>6} {len(g):>5} {g['r'].mean():>+8.3f} "
                  f"{100*(g['r']>0).mean():>6.1f}% {100*g['ca'].mean():>5.0f}% "
                  f"{100*g['res'].mean():>9.0f}%")
        lo = sub[sub["decile"] <= 3]["r"]
        hi = sub[sub["decile"] >= 8]["r"]
        prem = (lo.mean() if len(lo) else float('nan')) - (hi.mean() if len(hi) else float('nan'))
        print(f"    → illiquidity premium (D1-3 − D8-10) = {prem:+.3f} R/trade")
        return prem

    pooled = _table(df, "POOLED")

    print("\n  WALK-FORWARD (premium sign must persist across eras):")
    fold_prem = {}
    for e in ["2000-2010", "2011-2017", "2018-2026"]:
        sub = df[df["year"].map(_era) == e]
        if len(sub) < 30:
            print(f"    {e}: too few trades ({len(sub)})"); continue
        lo = sub[sub["decile"] <= 3]["r"]; hi = sub[sub["decile"] >= 8]["r"]
        p = (lo.mean() if len(lo) else float('nan')) - (hi.mean() if len(hi) else float('nan'))
        fold_prem[e] = p
        print(f"    {e}: premium {p:+.3f} R  (D1-3 n={len(lo)} E[R] {lo.mean():+.3f} | "
              f"D8-10 n={len(hi)} E[R] {hi.mean():+.3f})")

    signs = [1 if p > 0 else 0 for p in fold_prem.values()]
    print(f"\n  VERDICT (v1, pre-sector-neutralisation): pooled premium {pooled:+.3f}; "
          f"positive in {sum(signs)}/{len(signs)} eras.")
    print("  A candidate PASS still REQUIRES v2 sector-neutralisation (build config/sector_map.yaml).")
    print("  If the low-liquidity deciles above are ~all resource/CA, treat v1 as inconclusive.\n")


if __name__ == "__main__":
    main()
