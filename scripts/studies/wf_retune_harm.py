#!/usr/bin/env python3
"""
Is per-window re-tuning harmful out-of-sample?

Direct, controlled test. Two walk-forward passes over the SAME windows, differing
only in whether each IS window is re-tuned:

  no-retune   base config on IS and OOS
  re-tune     sweep IS, carry the IS-best config to OOS

The comparison metric is OOS E[R]. With 1yr OOS at a 12mo step the OOS blocks
tile the timeline without overlap, so window i is the same OOS period in both
passes and the per-window deltas are paired and non-overlapping.

Reads on the DSR/PBO "search is the enemy" verdict from a different direction: if
the IS-optimal config generalises WORSE than the untuned base config, the act of
searching per window is itself destroying edge, and the live system should not do
it. The classic overfit signature is re-tune IS >> no-retune IS while re-tune OOS
<= no-retune OOS.

Uses the corrected walk-forward (entry-cutoff tail + IS purge, now default), so
this is not re-measuring the old truncation artifact.

    python scripts/studies/wf_retune_harm.py --joint 20 --workers 14 \
        --snapshot data/snapshot_2026-06-10
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


def _wtd(points, leg: str) -> float:
    """Trade-weighted mean E[R] across window points: Σ(n·E[R]) / Σn."""
    n = sum(getattr(p, leg).stats.trades_count for p in points)
    if not n:
        return float("nan")
    return sum(getattr(p, leg).stats.trades_count * getattr(p, leg).stats.expectancy_r
               for p in points) / n


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import numpy as np
    import yaml
    from scipy import stats as sps

    from backtest.loader import load_universe
    from backtest.walk_forward import WalkForwardEngine

    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--step-months", type=int, default=12)
    ap.add_argument("--joint", type=int, default=20,
                    help="Joint configs sampled per IS window in the re-tune leg "
                         "(0 = OFAT). 20 matches the run that first flagged the effect.")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--workers", type=int, default=14)
    args = ap.parse_args()

    snap = _ROOT / args.snapshot
    base_cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    wl = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text(encoding="utf-8"))
    tickers = [t for t in wl.get("tier_a", []) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
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
        "max_open_risk": 5.0, "earnings_aware": True,
        "entry_slippage_pct": float(exec_cfg.get("entry_slippage_pct", 0.002)),
        "exit_slippage_pct": float(exec_cfg.get("exit_slippage_pct", 0.0) or 0.0),
        "commission_r": float(exec_cfg.get("commission_r", 0.005)),
        "close_open_at_eod": True,
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    }
    if exec_cfg.get("breakeven_trigger_r"):
        base_port["breakeven_trigger_r"] = float(exec_cfg["breakeven_trigger_r"])
        if exec_cfg.get("breakeven_buffer_atr"):
            base_port["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    def go(re_tune: bool):
        t0 = time.time()
        wfe = WalkForwardEngine(
            universe=uni, base_cfg=base_cfg, base_port_cfg=base_port,
            is_years=3, oos_years=1, step_months=args.step_months,
            re_tune=re_tune, n_workers=args.workers,
            joint_samples=(args.joint if re_tune else 0), joint_seed=args.seed,
        )
        rep = wfe.run(progress=(lambda m: print(f"    · {m}", flush=True))
                      if re_tune else None)
        print(f"  {'re-tune' if re_tune else 'no-retune'} leg: "
              f"{len(rep.results)} windows in {time.time() - t0:.0f}s", flush=True)
        return rep.results

    print("\n  no-retune leg (base config, cheap)…", flush=True)
    base_pts = go(re_tune=False)
    print("\n  re-tune leg (sweep per window, slow)…", flush=True)
    tuned_pts = go(re_tune=True)

    # Pair by window index (deterministic windows → same OOS period each side).
    n = min(len(base_pts), len(tuned_pts))
    base_pts, tuned_pts = base_pts[:n], tuned_pts[:n]

    b_is, b_oos = _wtd(base_pts, "is_point"), _wtd(base_pts, "oos_point")
    t_is, t_oos = _wtd(tuned_pts, "is_point"), _wtd(tuned_pts, "oos_point")

    print("\n" + "=" * 78)
    print("  IS RE-TUNING HARMFUL OUT-OF-SAMPLE?   (3yr IS / 1yr OOS / "
          f"{args.step_months}mo step, {n} windows)")
    print("=" * 78)
    print(f"  {'leg':<14}{'IS E[R]':>12}{'OOS E[R]':>12}{'IS→OOS drop':>14}")
    print(f"  {'no-retune':<14}{b_is:>+12.4f}{b_oos:>+12.4f}{b_is - b_oos:>+14.4f}")
    print(f"  {'re-tune':<14}{t_is:>+12.4f}{t_oos:>+12.4f}{t_is - t_oos:>+14.4f}")
    print("  " + "-" * 74)

    # Per-window paired OOS delta (re-tune − no-retune). OOS blocks tile without
    # overlap at this step, so these are ~independent paired observations.
    deltas, wins_for_retune = [], 0
    print(f"  {'window':<8}{'OOS period':<26}{'no-retune':>11}{'re-tune':>10}{'Δ OOS':>9}")
    for bp, tp in zip(base_pts, tuned_pts):
        d = tp.oos_point.stats.expectancy_r - bp.oos_point.stats.expectancy_r
        deltas.append(d)
        wins_for_retune += (d > 0)
        w = bp.window
        print(f"  W{w.index:<7}{str(w.oos_start) + '→' + str(w.oos_end):<26}"
              f"{bp.oos_point.stats.expectancy_r:>+11.3f}"
              f"{tp.oos_point.stats.expectancy_r:>+10.3f}{d:>+9.3f}")
    deltas = np.asarray(deltas)

    # Sign test (windows where re-tune's OOS beat no-retune's) + paired t on the
    # per-window OOS E[R] deltas.
    tstat, pval = sps.ttest_1samp(deltas, 0.0)
    print("  " + "-" * 74)
    print(f"  windows re-tune beat no-retune OOS : {wins_for_retune}/{n}")
    print(f"  mean per-window OOS Δ              : {deltas.mean():+.4f}  "
          f"(paired t={tstat:+.2f}, p={pval:.4f})")
    print(f"  trade-weighted OOS Δ (re − base)   : {t_oos - b_oos:+.4f}")

    print("\n  VERDICT")
    print("  " + "-" * 74)
    overfit_sig = t_is > b_is and t_oos <= b_oos
    if t_oos < b_oos and pval < 0.10:
        print("  >>> RE-TUNING IS HARMFUL. The IS-best config generalises WORSE than")
        print("      the untuned base config out of sample.")
        if overfit_sig:
            print(f"      Textbook overfit signature: re-tune lifts IS "
                  f"({b_is:+.3f}→{t_is:+.3f}) but sinks OOS ({b_oos:+.3f}→{t_oos:+.3f}).")
        print("      Implication: the live system should NOT re-tune per window; this")
        print("      is the DSR/PBO 'search is the enemy' finding shown directly.")
    elif t_oos < b_oos:
        print(f"  >>> Re-tune OOS is lower ({t_oos:+.4f} vs {b_oos:+.4f}) but the")
        print(f"      per-window difference is not distinguishable (p={pval:.3f}). Suggestive")
        print("      of harm, not conclusive — a second seed would sharpen it.")
    else:
        print(f"  >>> Re-tuning is NOT harmful here (OOS {t_oos:+.4f} vs {b_oos:+.4f}).")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
