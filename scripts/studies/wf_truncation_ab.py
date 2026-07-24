#!/usr/bin/env python3
"""
Walk-forward truncation A/B — what did the entry-cutoff fix change?

Three legs over the SAME universe load, production window settings
(3yr IS / 1yr OOS / 6mo step), re-tune OFF so the comparison isolates window
hygiene rather than per-window search:

  legacy   resolve_tail_bars=0            — bars truncated at the window edge;
                                            the purge is inert by construction
  fixed    tail + purge                   — entry cutoff separated from bar cutoff
  embargo  tail + purge + 25-bar embargo  — pre-registered gap (= max_hold_days)

The quantity under test is DEGRADATION (IS E[R] − OOS E[R]), which is what
walk-forward exists to produce and exactly what edge-truncation biased: the
force-closed count is ~the open-position count regardless of window length, so
the 1-year OOS leg lost a ~3x larger share of its trades than the 3-year IS leg.

Exploratory — does NOT journal. Read-only on the DB.

    python scripts/studies/wf_truncation_ab.py --snapshot data/snapshot_2026-06-10
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import yaml
    from backtest.loader import load_universe
    from backtest.walk_forward import WalkForwardEngine

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--embargo-bars", type=int, default=25)
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--re-tune", action="store_true",
                    help="Sweep each IS window and carry the winner to OOS. This is "
                         "where the purge bites hardest — it filters EVERY candidate, "
                         "so config selection itself changes. ~60x the work per window: "
                         "budget hours, not minutes, and use --workers.")
    ap.add_argument("--joint", type=int, default=0, metavar="N",
                    help="With --re-tune: N random multi-knob configs per window "
                         "instead of the ~60-config OFAT sweep. The cheap way to cover "
                         "the re-tune path (N=20 is ~3x faster than OFAT).")
    ap.add_argument("--workers", type=int, default=0, metavar="W",
                    help="Parallel workers for the per-window sweep (--re-tune only; "
                         "the no-retune path is sequential by construction).")
    ap.add_argument("--legs", default="legacy,fixed,embargo",
                    help="Comma-separated subset of legs to run. Each leg is a full "
                         "walk-forward pass, so drop 'embargo' to halve a --re-tune run.")
    ap.add_argument("--step-months", type=int, default=6, metavar="M",
                    help="Months between window starts (default 6, the production "
                         "setting). Under --re-tune a fresh worker pool is spawned and "
                         "the universe re-pickled PER WINDOW, so this dominates runtime: "
                         "12 roughly halves both the window count and the wall clock. It "
                         "also reduces IS/OOS overlap between adjacent windows, which "
                         "makes the per-window selections more independent.")
    args = ap.parse_args()
    want = {s.strip() for s in args.legs.split(",") if s.strip()}

    snap = _ROOT / args.snapshot
    base_cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", []) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    print(f"  Loading universe ({len(tickers)} tickers)…", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snap / "prices", earnings_dir=snap / "earnings_history",
        macro_dir=snap / "macro", behavioral_dir=snap / "behavioral",
        start_date=date.fromisoformat(args.start),
    )
    print(f"  {uni.summary()}", flush=True)

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 5.0,
        "earnings_aware": True,
        "entry_slippage_pct": float(exec_cfg.get("entry_slippage_pct", 0.002)),
        "exit_slippage_pct": float(exec_cfg.get("exit_slippage_pct", 0.0) or 0.0),
        "commission_r": float(exec_cfg.get("commission_r", 0.005)),
        "close_open_at_eod": True,
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    }
    be = exec_cfg.get("breakeven_trigger_r")
    if be:
        base_port["breakeven_trigger_r"] = float(be)
        if exec_cfg.get("breakeven_buffer_atr"):
            base_port["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    def leg(label, *, tail_bars, embargo):
        port = dict(base_port, resolve_tail_bars=tail_bars)
        t0 = time.time()
        wfe = WalkForwardEngine(
            universe=uni, base_cfg=base_cfg, base_port_cfg=port,
            is_years=3, oos_years=1, step_months=args.step_months,
            re_tune=args.re_tune,
            embargo_bars=embargo, n_workers=args.workers,
            joint_samples=args.joint,
        )
        rep = wfe.run(progress=(lambda m: print(f"    · {m}", flush=True))
                      if args.re_tune else None)
        rows = rep.results
        is_er = [r.is_point.stats.expectancy_r for r in rows]
        oos_er = [r.oos_point.stats.expectancy_r for r in rows]
        n_is = sum(r.is_point.stats.trades_count for r in rows)
        n_oos = sum(r.oos_point.stats.trades_count for r in rows)
        purged = sum(r.is_point.purged_trades for r in rows)
        trunc_is = sum(r.is_point.tail_truncated for r in rows)
        trunc_oos = sum(r.oos_point.tail_truncated for r in rows)
        eod_is = sum(1 for r in rows for t in r.is_point.trades if t.exit_reason == "open_eod")
        eod_oos = sum(1 for r in rows for t in r.oos_point.trades if t.exit_reason == "open_eod")
        # Trade-weighted is the headline. An unweighted mean of per-window E[R]
        # gives a 3-trade window the same vote as a 105-trade one, and windows
        # that thin are common (a 1-year OOS block in a quiet regime). Measured
        # 2026-07-22: the two disagreed by more than 2x on the degradation SHIFT
        # (-6.8% unweighted vs -2.9% weighted). Both are printed; the gap between
        # them is itself the read on window-size noise.
        mis, moos = _weighted(rows, "is_point"), _weighted(rows, "oos_point")
        umis, umoos = _mean(is_er), _mean(oos_er)
        # flush every line: a leg takes ~35 min and stdout is normally redirected
        # to a file, where block buffering would otherwise hide a completed leg's
        # numbers until the process exits.
        print(f"\n  {label}  ({len(rows)} windows, {time.time() - t0:.0f}s)", flush=True)
        print(f"    IS  {n_is:>5}t  E[R] {mis:+.4f} (unwtd {umis:+.4f})   "
              f"open_eod {eod_is:>4}  tail_trunc {trunc_is:>4}  purged {purged:>4}",
              flush=True)
        print(f"    OOS {n_oos:>5}t  E[R] {moos:+.4f} (unwtd {umoos:+.4f})   "
              f"open_eod {eod_oos:>4}  tail_trunc {trunc_oos:>4}", flush=True)
        print(f"    DEGRADATION (IS − OOS) : {mis - moos:+.4f}  "
              f"(unwtd {umis - umoos:+.4f})", flush=True)
        thin = sum(1 for r in rows if r.oos_point.stats.trades_count < 20)
        if thin:
            print(f"    ! {thin}/{len(rows)} OOS windows hold <20 trades — "
                  f"read the weighted figure", flush=True)
        return dict(label=label, n_is=n_is, n_oos=n_oos, is_er=mis, oos_er=moos,
                    deg=mis - moos, purged=purged, rows=rows)

    mode = ("re-tune ON ("
            + (f"joint {args.joint}/window" if args.joint else "OFAT")
            + f", workers={args.workers})") if args.re_tune else "re-tune OFF"
    print("\n" + "=" * 74)
    print(f"  Walk-forward truncation A/B — 3yr IS / 1yr OOS / "
          f"{args.step_months}mo step, {mode}")
    print("=" * 74)

    specs = [("legacy", "legacy  (truncate at edge)", 0, 0),
             ("fixed", "fixed   (tail + purge)", 252, 0),
             ("embargo", f"embargo (tail + purge + {args.embargo_bars}b)",
              252, args.embargo_bars)]
    done = {}
    for key, label, tail_bars, embargo in specs:
        if key in want:
            done[key] = leg(label, tail_bars=tail_bars, embargo=embargo)

    print("\n" + "=" * 74)
    print("  EFFECT OF THE FIX")
    print("  " + "-" * 70)
    print(f"  {'leg':<30} {'IS E[R]':>9} {'OOS E[R]':>9} {'degradation':>12}")
    for r in done.values():
        print(f"  {r['label']:<30} {r['is_er']:>+9.4f} {r['oos_er']:>+9.4f} {r['deg']:>+12.4f}")
    print("  " + "-" * 70)
    legacy, fixed, embar = done.get("legacy"), done.get("fixed"), done.get("embargo")
    if legacy and fixed:
        print(f"  truncation fix moved degradation by {fixed['deg'] - legacy['deg']:+.4f} "
              f"({_pct(legacy['deg'], fixed['deg'])})")
    if fixed and embar:
        print(f"  embargo moved it a further            {embar['deg'] - fixed['deg']:+.4f}")
    purged = "  ".join(f"{k}={r['purged']}" for k, r in done.items() if r['purged'])
    print(f"  IS trades purged                     : {purged or '0 (legacy leg only)'}")
    if args.re_tune:
        print("\n  Re-tune leg: the purge filtered every IS sweep candidate, so any")
        print("  change here is a change in which CONFIG each window selected — not")
        print("  just in how the same config scored.")
    print("\n  Read: degradation is the walk-forward verdict. A leg that truncates its")
    print("  1-year OOS window harder than its 3-year IS window reports a degradation")
    print("  that is an artifact of the window edge, not of the strategy.")
    print("=" * 74 + "\n")


def _weighted(rows, leg: str) -> float:
    """Trade-weighted mean E[R] across windows: Σ(n·E[R]) / Σn.

    Equivalent to pooling every trade and taking one expectancy, so a window
    contributes in proportion to the evidence it carries.
    """
    n = sum(getattr(r, leg).stats.trades_count for r in rows)
    if not n:
        return float("nan")
    return sum(getattr(r, leg).stats.trades_count * getattr(r, leg).stats.expectancy_r
               for r in rows) / n


def _mean(xs):
    xs = [x for x in xs if x == x]
    return sum(xs) / len(xs) if xs else float("nan")


def _pct(a, b):
    if not a:
        return "n/a"
    return f"{(b - a) / abs(a) * 100:+.1f}% relative"


if __name__ == "__main__":
    main()
