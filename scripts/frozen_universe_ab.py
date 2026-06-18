#!/usr/bin/env python3
"""
Frozen-universe A/B (survivorship / selection-bias audit).

Quantifies how much of the backtest edge is hindsight. For each as-of date D it
runs the SAME windowed backtest (D → present, shipped 25-bar hard cap) on:

  A (hindsight)  — the CURRENT tier_a (curated with full hindsight: losers deleted,
                   newer names kept).
  B (frozen)     — names that actually existed at D (inception <= D), INCLUDING the
                   deleted losers (re-added) and EXCLUDING names born after D.

The **selection discount** = A − B (Δ total-R / Sharpe / max-DD). It also lists the
look-ahead inclusions (in A, born after D) and the pruned losers re-added to B.

Reads `survivorship_audit` from config/watchlist.yaml. The pruned losers must be in
the price cache first — backfill with `scripts/fetch_prices.py`.

    python scripts/frozen_universe_ab.py
    python scripts/frozen_universe_ab.py --as-of 2010-01-01
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_cfg():
    import yaml
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tier_a = [t for t in wl.get("tier_a", []) if isinstance(t, str)]
    audit = wl.get("survivorship_audit", {}) or {}
    return base_cfg, tier_a, audit


def _metrics(trades) -> dict:
    from backtest.equity_curve import build_curve
    closed = [t for t in trades if t.exit_date is not None]
    rs = [t.effective_r for t in closed]
    n = len(rs)
    ec = build_curve(closed)
    return {
        "n": n,
        "wr": (100 * sum(1 for r in rs if r > 0) / n) if n else 0.0,
        "total": ec.total_r,
        "sharpe": ec.sharpe,
        "max_dd": ec.max_dd,
    }


def _run_subset(uni, base_cfg, base_port, tickers, start, end) -> dict:
    from backtest.sweep import SweepEngine
    sub_prepped = {t: uni.prepped[t] for t in tickers if t in uni.prepped}
    if not sub_prepped:
        return {"n": 0, "wr": 0.0, "total": 0.0, "sharpe": float("nan"), "max_dd": 0.0}
    sub = replace(uni, prepped=sub_prepped)          # keep market/vix/macro/behavioral context
    bp = dict(base_port)
    bp["start_date"] = start
    bp["end_date"] = end
    eng = SweepEngine(universe=sub, base_cfg=base_cfg, base_port_cfg=bp, n_workers=1)
    return _metrics(eng.baseline().trades)


def _ab_job(d, last, tickers, base_cfg, base_port) -> dict:
    """ProcessPool worker: one subset backtest on the per-worker cached universe
    (shipped once per worker via backtest.sweep's _worker_init/_pack_universe)."""
    import backtest.sweep as _sweep
    uni = _sweep._WORKER_UNIVERSE
    if uni is None:
        raise RuntimeError("worker universe not initialised (initargs missing)")
    return _run_subset(uni, base_cfg, base_port, tickers, d, last)


def _f(v, d=2):
    return "∞" if (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{d}f}"


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Frozen-universe survivorship A/B")
    ap.add_argument("--as-of", nargs="+", default=None, metavar="YYYY-MM-DD",
                    help="As-of date(s); default: survivorship_audit.as_of_dates in watchlist.yaml")
    ap.add_argument("--sleeve", choices=["all", "to", "us"], default="all",
                    help="restrict the audit to one venue sleeve (.TO vs US) — "
                         "per-sleeve selection discount (B3 follow-up)")
    ap.add_argument("--snapshot", default=None, metavar="DIR",
                    help="frozen data snapshot (e.g. data/snapshot_2026-06-10); "
                         "default: live caches")
    ap.add_argument("--workers", type=int,
                    default=min(6, max(1, (os.cpu_count() or 4) - 2)),
                    help="A/B subset backtests run in parallel across this many "
                         "processes (the 2×as-of jobs are independent); 1 = sequential")
    args = ap.parse_args()

    base_cfg, tier_a, audit = _load_cfg()
    pruned = [t for t in (audit.get("pruned_losers") or []) if isinstance(t, str)]
    # The sleeve scopes the A/B name sets ONLY. The load list stays the full
    # union: the loader segregates the market-context symbols (SPY/QQQ/^VIX)
    # out of whatever list it is given, and a .TO-only list would drop them —
    # no index data → regime defaults to CHOP → every entry blocked.
    load_list = list(dict.fromkeys(tier_a + pruned))
    if args.sleeve != "all":
        is_to = lambda t: t.endswith(".TO")  # noqa: E731
        keep = is_to if args.sleeve == "to" else (lambda t: not is_to(t))
        tier_a = [t for t in tier_a if keep(t)]
        pruned = [t for t in pruned if keep(t)]
    as_of_strs = args.as_of or audit.get("as_of_dates") or ["2010-01-01"]
    as_of_dates = [datetime.strptime(s, "%Y-%m-%d").date() for s in as_of_strs]

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 5.0,
        "earnings_aware": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
        "max_hold_days": int(audit.get("max_hold_days", 25)),
        "max_hold_mode": str(audit.get("max_hold_mode", "hard")).replace("-", "_"),
    }

    from backtest.loader import load_universe
    candidates = list(dict.fromkeys(tier_a + pruned))  # union, order-preserving

    if args.snapshot:
        snap = Path(args.snapshot)
        if not snap.is_absolute():
            snap = _ROOT / snap
        load_dirs = dict(cache_dir=snap / "prices",
                         earnings_dir=snap / "earnings_history",
                         macro_dir=snap / "macro",
                         behavioral_dir=snap / "behavioral")
    else:
        load_dirs = dict(cache_dir=_ROOT / "data" / "prices",
                         earnings_dir=_ROOT / "data" / "earnings_history")

    print(f"\n  Survivorship A/B  ·  sleeve={args.sleeve}  ·  "
          f"cap={base_port['max_hold_days']}d {base_port['max_hold_mode']}"
          f"  ·  as-of: {', '.join(as_of_strs)}")
    if args.snapshot:
        print(f"  Snapshot: {load_dirs['cache_dir'].parent}")
    print(f"  Loading {len(load_list)} tickers (full union — the sleeve scopes "
          f"A/B only; {len(candidates)} in-sleeve)…", flush=True)
    t0 = time.time()
    uni = load_universe(
        load_list,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        **load_dirs,
    )
    print(f"  {uni.summary()}  ({time.time() - t0:.1f}s)")

    incept = {t: p.df.index[0].date() for t, p in uni.prepped.items()}
    last = uni.date_range.last
    missing_pruned = [t for t in pruned if t not in uni.prepped]
    if missing_pruned:
        print(f"  ⚠ pruned losers WITHOUT cached data (excluded — run "
              f"scripts/fetch_prices.py {' '.join(missing_pruned)}): {missing_pruned}")

    specs = []
    for D in as_of_dates:
        A = [t for t in tier_a if t in uni.prepped]                      # hindsight (current)
        B = [t for t in candidates if t in uni.prepped and incept[t] <= D]  # frozen as-of D
        lookahead = sorted(t for t in A if incept[t] > D)               # in A, born after D
        readd = sorted(t for t in pruned if t in uni.prepped and incept[t] <= D)
        specs.append((D, A, B, lookahead, readd))

    # The 2×as-of subset backtests are independent — fan them out across a
    # process pool (universe shipped once per worker, sweep's pack/init).
    results: dict = {}
    if args.workers > 1 and len(specs) > 1:
        from concurrent.futures import ProcessPoolExecutor
        from backtest.sweep import _pack_universe, _worker_init
        jobs = [(D, leg, names) for D, A, B, _, _ in specs
                for leg, names in (("A", A), ("B", B))]
        n_workers = min(args.workers, len(jobs))
        print(f"\n  Running {len(jobs)} subset backtests across "
              f"{n_workers} workers…", flush=True)
        packed = _pack_universe(uni)
        with ProcessPoolExecutor(max_workers=n_workers, initializer=_worker_init,
                                 initargs=(str(_ROOT), packed)) as pool:
            futs = {(D, leg): pool.submit(_ab_job, D, last, names,
                                          base_cfg, base_port)
                    for D, leg, names in jobs}
            results = {k: f.result() for k, f in futs.items()}

    rows = []
    for D, A, B, lookahead, readd in specs:
        print(f"\n  ── as-of {D}  ·  test window {D} → {last} ──")
        print(f"     A hindsight: {len(A)} names | B frozen: {len(B)} names "
              f"| look-ahead inclusions: {len(lookahead)} | pruned re-added: {len(readd)}")
        rA = results.get((D, "A")) or _run_subset(uni, base_cfg, base_port, A, D, last)
        rB = results.get((D, "B")) or _run_subset(uni, base_cfg, base_port, B, D, last)
        rows.append((D, rA, rB, lookahead, readd))
        print(f"     A  {rA['n']:4d}t  TotalR {rA['total']:+7.1f}  Sharpe {_f(rA['sharpe'])}  maxDD {rA['max_dd']:.1f}")
        print(f"     B  {rB['n']:4d}t  TotalR {rB['total']:+7.1f}  Sharpe {_f(rB['sharpe'])}  maxDD {rB['max_dd']:.1f}")
        print(f"     selection discount  ΔTotalR {rB['total']-rA['total']:+7.1f}  "
              f"ΔSharpe {_f(rB['sharpe']-rA['sharpe'])}")
        if lookahead:
            print(f"     look-ahead names: {', '.join(lookahead)}")
        if readd:
            print(f"     pruned re-added : {', '.join(readd)}")

    print("\n" + "=" * 78)
    print(f"  {'As-of':<12} {'A TotalR':>9} {'B TotalR':>9} {'ΔR (B-A)':>9} "
          f"{'A SR':>6} {'B SR':>6} {'look-ahead':>11} {'re-add':>7}")
    print("  " + "-" * 74)
    for D, rA, rB, la, ra in rows:
        print(f"  {str(D):<12} {rA['total']:>+9.1f} {rB['total']:>+9.1f} "
              f"{rB['total']-rA['total']:>+9.1f} {_f(rA['sharpe']):>6} {_f(rB['sharpe']):>6} "
              f"{len(la):>11} {len(ra):>7}")
    print("=" * 78)
    print("\n  ΔR (B−A) < 0 ⇒ the hindsight universe (A) out-earns the as-of-honest")
    print("  universe (B): that gap is the SELECTION DISCOUNT — edge attributable to")
    print("  survivorship/look-ahead, not to the strategy. A small gap is reassuring.\n")


if __name__ == "__main__":
    main()
