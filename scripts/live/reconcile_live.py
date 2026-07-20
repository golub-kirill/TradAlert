#!/usr/bin/env python3
"""
Live-vs-backtest reconciliation — is the strategy tracking expectancy NOW?

Pulls fired entry signals from the live journal (`scan_results` + `scan_runs`),
replays each one forward against the cached price history through the SAME exit
ladder the backtester uses — stop / target / max-hold / breakeven ratchet /
engine-exit — via the shared `backtest.counterfactual.replay_counterfactual`,
then scores the tradeable book (NOT the raw alert stream) against
`backtest_trades` expectancy. Flags drift > ±0.15 R/trade.

The replay is the same module the opportunity tracker uses, and a faithful mirror
of the friction-bearing `PortfolioBacktester` that journals the reference run:
entry AND exit slippage, commission, and the breakeven ratchet
(`execution.breakeven_trigger_r`) all apply, and R is taken on the initial stop.

WHY THIS IS NOT A NAIVE PER-ALERT METER (the 2026-07-11 fix)
───────────────────────────────────────────────────────────
The live scanner is an *alerter*: it re-alerts the same names every day with no
memory of what is held. Scoring every alert as an independent full-risk trade is
NOT what the strategy (or the backtester) does and badly over-weights re-fires and
correlated clusters. So before scoring we replay each fire the way it would be
TRADED:
  • one position per ticker  — a re-fire while a prior position on that ticker is
    still open is skipped (the backtester holds; it never re-enters).
  • open-risk budget          — at most `risk.max_open_risk` concurrent positions
    (approximated at size_mult=1, chronological fill order).
  • hold to the real exit     — each held position runs the full exit ladder
    (breakeven ratchet + the regime-flip / momentum-fade / mean-rev engine chain)
    and fills at the next bar's open, exactly like the backtester, so the meter
    captures the runners the bare 25-bar cap would truncate (those `engine_exit`
    winners ARE the edge).

Scoring (the "Both" plan):
  • New rows (post-migration) carry stop_price/target_price/signal_type → scored exactly.
  • Old rows (geometry NULL) → stop/target reconstructed from close+atr+config (mode A).

A signal is *resolved* once it hits stop/target or reaches the cap; otherwise it's
*pending* (too recent — not enough forward bars yet). Reconciliation only judges
resolved, traded signals; pending ones accrue as the live feed matures.

    python scripts/live/reconcile_live.py
    python scripts/live/reconcile_live.py --max-hold-days 25 --drift 0.15

Requires DB_* in config/secrets.env (same as the live scanner) and the price cache.
Read-only on the DB.
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

# Load DB_* (and other secrets) into the environment so persistence.db_conn sees
# them — mirrors main.py / run_backtest.py, which load this explicitly at startup.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


def _max_open_risk() -> int:
    """Concurrent-position budget (settings.risk.max_open_risk), floored to an int
    slot count (approximates the size_mult budget at full size). 0/absent → no cap."""
    import yaml
    try:
        with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            s = yaml.safe_load(f) or {}
        return int(float((s.get("risk") or {}).get("max_open_risk", 5.0)))
    except Exception:
        return 5


def _ref_run(cur, bt_run_id):
    """Expectancy reference = the given backtest_runs id, else the latest scoring-OFF
    FULL-WINDOW run (matching the live default; a windowed diagnostic can't hijack it),
    else the newest overall. The chosen run is always printed by the caller so the
    provenance is visible."""
    from backtest.db import reference_run
    return reference_run(cur, bt_run_id)


def _has_tier_column(cur) -> bool:
    """True when scan_results carries the live-freshness ``tier`` column (added by
    data/scan_results_tier_migration.sql). Lets the reconciler run unchanged against an
    un-migrated DB — it simply can't exclude NEEDS_REVIEW fires until the column exists."""
    try:
        cur.execute(
            "SELECT COUNT(*) n FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'scan_results' "
            "AND column_name = 'tier'"
        )
        row = cur.fetchone()
        n = list(row.values())[0] if isinstance(row, dict) else row[0]
        return bool(n)
    except Exception:
        return False


def _fetch(conn, bt_run_id=None):
    cur = conn.cursor(dictionary=True)
    ref = _ref_run(cur, bt_run_id)
    if ref is None:
        cur.close()
        return [], {"__ALL__": (0.0, 0)}, None, 0
    rid = ref["id"]
    # Exclude NEEDS_REVIEW fires (stale/gapped data) from the drift meter — they were never
    # clean LIVE signals. The IS NULL arm keeps pre-migration rows (backfilled to 'LIVE') in.
    has_tier = _has_tier_column(cur)
    tier_filter = " AND (sr.tier IS NULL OR sr.tier = 'LIVE') " if has_tier else " "
    cur.execute(
        "SELECT sr.id, sr.ticker, sr.signal_kind, sr.close, sr.atr, "
        "       sr.stop_price, sr.target_price, sr.signal_type, "
        "       r.created_at, r.market_regime "
        "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
        "WHERE sr.passed = 1 AND sr.signal_kind IN ('entry_long','entry_short')"
        + tier_filter +
        "ORDER BY r.created_at, sr.ticker"
    )
    sigs = cur.fetchall()
    # Count the NEEDS_REVIEW entries held out, so the exclusion is visible rather than silent.
    needs_review = 0
    if has_tier:
        cur.execute(
            "SELECT COUNT(*) n FROM scan_results sr "
            "WHERE sr.passed = 1 AND sr.signal_kind IN ('entry_long','entry_short') "
            "AND sr.tier = 'NEEDS_REVIEW'"
        )
        row = cur.fetchone()
        needs_review = int(list(row.values())[0] if isinstance(row, dict) else row[0])
    # Compare LIKE-FOR-LIKE: the live replay computes per-unit R (it can't know the
    # portfolio size or borrow accrual at alert time), so aggregate the backtest side
    # on per-unit r_multiple too — NOT size-scaled effective_r (audit M6). COALESCE the
    # signal_type bucket so the backtest side groups identically to the live side
    # (which maps NULL → 'momentum'); otherwise NULL-typed trades land in a bucket
    # live signals can never match.
    rcol = "r_multiple"
    cur.execute(
        f"SELECT market_regime, COALESCE(signal_type,'momentum') signal_type, "
        f"COUNT(*) n, AVG({rcol}) exp_r "
        "FROM backtest_trades WHERE run_id = %s "
        "GROUP BY market_regime, COALESCE(signal_type,'momentum')",
        (rid,),
    )
    exp = {(row["market_regime"], row["signal_type"]): (float(row["exp_r"]), int(row["n"]))
           for row in cur.fetchall()}
    cur.execute(f"SELECT AVG({rcol}) e FROM backtest_trades WHERE run_id = %s", (rid,))
    overall = cur.fetchone()
    exp["__ALL__"] = (float(overall["e"]) if overall and overall["e"] is not None else 0.0, 0)
    cur.close()
    return sigs, exp, ref, needs_review


def _select_traded(resolved, cap):
    """Reduce resolved fires to the book a disciplined trader / the backtester would
    actually hold: one position per ticker at a time + at most ``cap`` concurrent
    positions, filled in chronological entry order. ``cap`` <= 0 → budget disabled.

    ``resolved`` items need ``entry_date`` and ``exit_date`` (``datetime.date``). A
    position is "still open" on a later entry day D when its ``exit_date`` > D (an exit
    on/at D frees the slot first, matching the backtester's open-before-entry order)."""
    taken = []
    open_pos: dict[str, object] = {}  # ticker -> exit_date of the held position
    for f in sorted(resolved, key=lambda x: (x["entry_date"], x["ticker"])):
        d = f["entry_date"]
        open_pos = {tk: xd for tk, xd in open_pos.items() if xd > d}  # free exited slots
        if f["ticker"] in open_pos:
            continue  # one position per ticker — skip the re-fire
        if cap and len(open_pos) >= cap:
            continue  # open-risk budget full
        taken.append(f)
        open_pos[f["ticker"]] = f["exit_date"]
    return taken


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Live-vs-backtest reconciliation")
    ap.add_argument("--max-hold-days", type=int, default=None,
                    help="Time-stop cap in trading bars (default: execution.max_hold_days).")
    ap.add_argument("--max-hold-mode", default=None, choices=["hard", "if-not-profit"],
                    help="Time-stop mode (default: execution.max_hold_mode).")
    ap.add_argument("--drift", type=float, default=0.15, help="alert threshold, R/trade")
    ap.add_argument("--bt-run-id", type=int, default=None,
                    help="backtest_runs.id to use as the expectancy reference "
                         "(default: the latest scoring-OFF full-window run; printed).")
    ap.add_argument("--every-alert", action="store_true",
                    help="score EVERY fired alert as an independent trade (the old naive "
                         "meter) instead of the tradeable book — for diagnosing re-fire bias.")
    args = ap.parse_args()

    import pandas as pd
    from persistence.db_conn import connect
    from persistence.cache import load as cache_load
    from core.indicators.indicators import attach_indicators
    from core.filter_engine import FilterEngine
    from backtest.counterfactual import (load_market_context,
                                         make_engine_exit_probe,
                                         replay_config, replay_counterfactual)

    # Shared with the opportunity tracker: the exact exit ladder, friction and
    # market context the portfolio backtester used to journal the reference run.
    cfg = replay_config(_ROOT)
    max_hold = args.max_hold_days if args.max_hold_days is not None else cfg["max_hold_days"]
    max_hold_mode = (args.max_hold_mode or cfg["max_hold_mode"]).replace("-", "_")
    cap = 0 if args.every_alert else _max_open_risk()
    engine = None if args.every_alert else FilterEngine.from_dict(cfg["raw"])
    market_dfs, vix_df = (None, None) if args.every_alert else load_market_context(_ROOT)
    slice_cache: dict = {}  # engine-slice memo shared across every replayed fire

    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env."); return
    try:
        sigs, exp, ref, needs_review = _fetch(conn, args.bt_run_id)
    finally:
        conn.close()

    if ref is None:
        print("  No backtest_runs to reference — journal a backtest first "
              "(python -m backtest.run_backtest)."); return
    if not sigs:
        if needs_review:
            print(f"  All {needs_review} fired entry signal(s) are NEEDS_REVIEW (stale/gapped "
                  f"data) — excluded from reconciliation. Nothing clean to score yet.")
        else:
            print("  No fired entry signals in scan_results yet — run main.py to build the feed.")
        return

    dates = [s["created_at"] for s in sigs]
    bt_all = exp.get("__ALL__", (0.0, 0))[0]
    excl = f"  ·  {needs_review} NEEDS_REVIEW excluded" if needs_review else ""
    book = "EVERY-ALERT (naive)" if args.every_alert else f"tradeable book (1/ticker · {cap}R budget)"
    print(f"\n  Live reconciliation  ·  {len(sigs)} fired entry signals  ·  "
          f"{min(dates):%Y-%m-%d} → {max(dates):%Y-%m-%d}  ·  cap {max_hold}d {max_hold_mode}{excl}")
    print(f"  Scoring: {book}  ·  exits: stop/target/time-stop/breakeven"
          f"{'' if args.every_alert else '/engine-exit'}")
    print(f"  Expectancy reference: backtest_runs id={ref['id']} "
          f"({ref['start_date']}→{ref['end_date'] or 'latest'}, {ref['trades_count']} trades, "
          f"E[R] {bt_all:+.3f}, notes={ref['notes'] or '—'})\n")

    fires = []  # resolved fires: dict(regime, signal_type, r, reason, ticker, entry_date, exit_date)
    pending = 0
    errors = 0
    reconstructed = 0
    prepped: dict[str, "pd.DataFrame"] = {}  # ticker -> indicator-ready df (re-fires reuse)
    for s in sigs:
        tk = s["ticker"]
        df = prepped.get(tk)
        if df is None:
            try:
                df = attach_indicators(cache_load(tk))
            except Exception:
                errors += 1
                continue
            prepped[tk] = df
        D = pd.Timestamp(s["created_at"]).normalize()
        entry_idx = int(df.index.searchsorted(D, side="right"))  # first bar strictly after scan date = T+1
        if entry_idx >= len(df):
            pending += 1
            continue
        is_short = s["signal_kind"] == "entry_short"
        close_d = (float(s["close"]) if s["close"] is not None
                   else float(df.iloc[entry_idx]["open"]))
        atr = float(s["atr"]) if s["atr"] is not None else 0.0

        # exact geometry when stored; else reconstruct from close+atr+config (mode A).
        # Reconstruction uses the JOURNALED close, so it is passed to the replay
        # explicitly rather than rebuilt from the frame's bar-T close.
        if s["stop_price"] is not None and s["target_price"] is not None:
            stop, target = float(s["stop_price"]), float(s["target_price"])
        else:
            reconstructed += 1
            if is_short:
                stop = close_d + atr * cfg["atr_mult"]
                target = close_d - (stop - close_d) * cfg["min_rr"]
            else:
                stop = close_d - atr * cfg["atr_mult"]
                target = close_d + (close_d - stop) * cfg["min_rr"]

        # Replay through the shared exit ladder — the same code the opportunity
        # tracker uses and a faithful mirror of the portfolio backtester's held
        # path: entry slippage + target re-anchor, stop/target, max-hold, the
        # breakeven ratchet (execution.breakeven_trigger_r), exit slippage on
        # market fills, commission, and the engine exit chain via the probe. R is
        # computed on the initial stop, so this is directly comparable to the
        # backtest_trades reference.
        probe = (make_engine_exit_probe(engine, tk, df, market_dfs, vix_df,
                                        is_short=is_short, slice_cache=slice_cache)
                 if engine is not None else None)
        cf = replay_counterfactual(
            df, signal_idx=entry_idx - 1, ticker=tk,
            direction="short" if is_short else "long",
            stop_price=stop, target_price=target, min_rr=cfg["min_rr"],
            max_hold_days=max_hold, max_hold_mode=max_hold_mode,
            breakeven_trigger_r=cfg["breakeven_trigger_r"],
            breakeven_buffer_atr=cfg["breakeven_buffer_atr"],
            commission_r=cfg["commission_r"],
            entry_slippage_pct=cfg["entry_slippage_pct"],
            exit_slippage_pct=cfg["exit_slippage_pct"],
            exit_probe=probe)
        if cf is None or cf.gapped_through:
            # None = degenerate signal bar (warmup); gapped_through = risk <= 0 at
            # the fill. Both were skipped as errors by the pre-migration guard.
            errors += 1
            continue
        if not cf.matured:
            # Ran off the end of the data — not enough forward bars to resolve yet.
            pending += 1
            continue
        st = s["signal_type"] or "momentum"
        fires.append({
            "regime": s["market_regime"], "signal_type": st, "r": cf.r_multiple,
            "reason": cf.exit_reason, "ticker": tk, "mfe_r": cf.mfe_r,
            "entry_date": df.index[cf.entry_idx].date(),
            "exit_date": df.index[cf.exit_idx].date(),
        })

    # Reduce the raw fire stream to the tradeable book (one position per ticker +
    # open-risk budget) unless --every-alert asked for the naive per-alert meter.
    traded = fires if args.every_alert else _select_traded(fires, cap)
    dropped = len(fires) - len(traded)
    print(f"  Resolved fires: {len(fires)}   Traded (scored): {len(traded)}"
          f"   Pending (too recent): {pending}   Errors/skipped: {errors}")
    if dropped:
        print(f"  ({dropped} re-fire/over-budget fires collapsed into open positions; "
              f"geometry reconstructed for {reconstructed})")
    elif reconstructed:
        print(f"  (geometry reconstructed for {reconstructed})")
    resolved = traded
    if not resolved:
        print("\n  ⚠ Nothing matured yet — the live feed is too young to score outcomes.")
        print("    Keep the scanner running daily; rerun this once signals age ~25 trading days.\n")
        return

    # aggregate by regime
    by_reg = defaultdict(list)
    for f in resolved:
        by_reg[(f["regime"], f["signal_type"])].append(f["r"])
    allr = [f["r"] for f in resolved]
    live_all = sum(allr) / len(allr)

    print("\n" + "=" * 78)
    print(f"  {'Regime / type':<24} {'live n':>6} {'live E[R]':>9} {'bt E[R]':>8} "
          f"{'drift':>7}  flag")
    print("  " + "-" * 74)
    for key in sorted(by_reg):
        rs = by_reg[key]
        le = sum(rs) / len(rs)
        be = exp.get(key, (None, 0))[0]
        if be is None:
            print(f"  {key[0] + '/' + key[1]:<24} {len(rs):>6} {le:>+9.3f} {'  n/a':>8} {'':>7}")
            continue
        drift = le - be
        flag = "  ⚠ DRIFT" if abs(drift) > args.drift else "  ok"
        print(f"  {key[0] + '/' + key[1]:<24} {len(rs):>6} {le:>+9.3f} {be:>+8.3f} {drift:>+7.3f}{flag}")
    print("  " + "-" * 74)
    drift_all = live_all - bt_all
    print(f"  {'ALL':<24} {len(allr):>6} {live_all:>+9.3f} {bt_all:>+8.3f} {drift_all:>+7.3f}"
          f"{'  ⚠ DRIFT' if abs(drift_all) > args.drift else '  ok'}")
    # win rate + exit mix + excursion capture (MFE/MAE now come from the shared
    # ladder — the reconciler could not measure these before the migration).
    wr = 100 * sum(1 for r in allr if r > 0) / len(allr)
    mix = defaultdict(int)
    for f in resolved:
        mix[f["reason"]] += 1
    print(f"  live WR {wr:.0f}%   exits: " + ", ".join(f"{k}={v}" for k, v in sorted(mix.items())))
    mfes = [f["mfe_r"] for f in resolved if f.get("mfe_r") is not None]
    if mfes:
        import statistics
        gave_back = [f["r"] / f["mfe_r"] for f in resolved
                     if f.get("mfe_r") and f["mfe_r"] > 0]
        capture = (f"{statistics.median(gave_back):.0%} median capture"
                   if gave_back else "n/a")
        print(f"  avg MFE {sum(mfes) / len(mfes):+.2f}R   exit vs MFE: {capture}")
    print("=" * 78)
    if abs(drift_all) > args.drift:
        print(f"\n  ⚠ Live is drifting {drift_all:+.3f} R/trade from backtest expectancy "
              f"(|drift| > {args.drift}). Investigate before trusting the edge live.\n")
    else:
        print(f"\n  ✓ Live tracking backtest within ±{args.drift} R/trade on resolved signals.\n")


if __name__ == "__main__":
    main()
