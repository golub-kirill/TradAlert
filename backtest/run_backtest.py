#!/usr/bin/env python3
"""
TradAlert Backtest Runner — CLI entry point.

Usage
─────
    python backtest/run_backtest.py                         # baseline only
    python backtest/run_backtest.py --sweep                 # full OFAT sweep
    python backtest/run_backtest.py --sweep --quick         # reduced grid
    python backtest/run_backtest.py --sweep --workers 4     # parallel
    python backtest/run_backtest.py --start 2022-01-01      # date window
    python backtest/run_backtest.py --start 2020-01-01 --end 2022-12-31
    python backtest/run_backtest.py --tickers MSFT GOOGL TSLA  # subset
    python backtest/run_backtest.py --walk-forward          # IS/OOS validation
    python backtest/run_backtest.py --mean-rev-tune         # mean-rev sweep
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in [str(_ROOT), str(_ROOT / "src")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log)

    t_wall = time.time()

    import yaml
    from backtest.loader import load_universe
    from backtest.report import (
        print_baseline, print_report, save_html, save_csv,
        print_walk_forward, print_mean_rev_tune,
    )
    from backtest.sweep import SweepEngine, PORTFOLIO_GRID, MEAN_REV_GRID
    from backtest.equity_curve import build_curve, attribution_table
    from backtest.stats_utils import bootstrap_all, kelly_fraction, consecutive_loss_stats
    from backtest.walk_forward import WalkForwardEngine

    cfg_path = _ROOT / "config" / "filters.yaml"
    wl_path = _ROOT / "config" / "watchlist.yaml"
    if not cfg_path.exists(): _die(f"filters.yaml not found at {cfg_path}")
    if not wl_path.exists():  _die(f"watchlist.yaml not found at {wl_path}")

    with open(cfg_path, encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(wl_path, encoding="utf-8") as f:
        wl_tickers = yaml.safe_load(f)["tickers"]

    if args.tickers:
        ctx = [t for t in wl_tickers if t.upper() in ("SPY", "QQQ", "^VIX")]
        tickers = list(dict.fromkeys(args.tickers + ctx))
        print(f"\n  TradAlert Backtester  ·  {len(args.tickers)} tickers (CLI override)")
    else:
        tickers = wl_tickers
        print(f"\n  TradAlert Backtester  ·  {len(tickers)} watchlist tickers")
    print(f"  Config: {cfg_path}")

    from datetime import datetime as _datetime
    def _parse_date(s):
        if not s: return None
        try:
            return _datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            _die(f"Invalid date '{s}' — expected YYYY-MM-DD")

    start_date = _parse_date(args.start)
    end_date = _parse_date(args.end)
    if start_date:
        print(f"  Window:  {start_date} → {end_date or 'latest'}")

    print("\n  Loading universe…", end="", flush=True)
    t_load = time.time()
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,  # always load history; earnings_buffer_days sweep
        # has zero effect when this is False because
        # prepped[ticker].earnings_history stays [] and
        # next_earn is always None inside call_engine_slice
        cache_dir=_ROOT / "data" / "prices",
        earnings_dir=_ROOT / "data" / "earnings_history",
        start_date=start_date,
        end_date=end_date,
    )
    print(f" done in {time.time() - t_load:.1f}s")
    print(f"  {uni.summary()}")
    if start_date and uni.date_range.first > start_date:
        print(f"  ⚠  Requested --start {start_date} predates available data "
              f"— earliest bar: {uni.date_range.first}")
    if uni.skipped:
        for ticker, reason in uni.skipped.items():
            print(f"  ⚠  {ticker}: {reason}")

    if uni.n_tradeable == 0:
        _die("No tradeable tickers loaded — check data/prices/ directory.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_concurrent": 5,
        "earnings_aware": True,  # must match load_universe(earnings_aware=True);
        # run_all() calls _prepare() which respects this flag
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.001),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
    }

    engine = SweepEngine(
        universe=uni,
        base_cfg=base_cfg,
        base_port_cfg=base_port,
        n_workers=max(args.workers, 0),
    )

    def _progress(msg: str) -> None:
        print(f"  ▸ {msg}", flush=True)

    # ── mean-reversion focused sweep ───────────────────────────────────────
    if args.mean_rev_tune:
        print(f"\n  Mean-reversion tuning sweep: {len(MEAN_REV_GRID)} params\n")
        bl = engine.baseline()
        report = engine.run_ofat(grid=MEAN_REV_GRID, port_grid=[], progress=_progress)
        print_mean_rev_tune(report, baseline_er=bl.stats.expectancy_r)
        if not args.no_csv:
            sp, tp = save_csv(report, out_dir)
            print(f"  Saved: {sp}")
        if not args.no_html:
            hp = save_html(report, out_dir / "mean_rev_report.html")
            print(f"  Saved: {hp}")

    # ── baseline only ──────────────────────────────────────────────────────
    elif not args.sweep:
        print("\n  Running baseline…", end="", flush=True)
        t0 = time.time()
        pt = engine.baseline()
        print(f" done in {time.time() - t0:.1f}s")

        trades = pt.trades
        ec = build_curve(trades) if trades else None
        rs = [t.r_multiple for t in trades if t.r_multiple is not None]
        boots = bootstrap_all(rs) if len(rs) >= 10 else None
        kel = (kelly_fraction(pt.stats.win_rate,
                              pt.stats.avg_winner_r,
                              abs(pt.stats.avg_loser_r))
               if rs else None)
        stks = consecutive_loss_stats(rs) if rs else None
        attr = attribution_table(trades) if trades else None

        wf_report = None
        if args.walk_forward:
            print("\n  Running walk-forward validation…")
            wfe = WalkForwardEngine(
                universe=uni,
                base_cfg=base_cfg,
                base_port_cfg=base_port,
                is_years=3,
                oos_years=1,
                step_months=6,
            )
            wf_report = wfe.run(progress=_progress)

        print_baseline(pt, equity=ec, bootstrap=boots,
                       kelly=kel, attribution=attr, streaks=stks)
        if wf_report:
            print_walk_forward(wf_report)

        # ── optional SQL journaling ────────────────────────────────────────
        if args.journal:
            _journal_baseline(pt, trades, base_cfg, start_date, end_date, uni)

        from backtest.sweep import SweepReport
        dummy = SweepReport(
            baseline=pt,
            points=[],
            universe_info=uni.summary(),
            elapsed_s=time.time() - t_wall,
            n_workers=0,
        )
        if not args.no_csv:
            sp, tp = save_csv(dummy, out_dir)
            print(f"  Saved: {sp}")
            print(f"  Saved: {tp}")
        if not args.no_html:
            hp = save_html(dummy, out_dir / "backtest_report.html",
                           equity=ec, wf_report=wf_report,
                           bootstrap=boots, kelly=kel,
                           attribution=attr, streaks=stks)
            print(f"  Saved: {hp}")

    # ── full OFAT sweep ────────────────────────────────────────────────────
    else:
        param_grid = _build_grid(args.quick)
        n_jobs = sum(len(spec.values) - 1 for spec in param_grid)
        print(f"\n  Sweep: {len(param_grid)} params × values = ~{n_jobs} runs")
        print(f"  Workers: {engine._n_workers}  |  Est. time: "
              f"~{n_jobs * 30 / max(engine._n_workers, 1) / 60:.0f} min\n")

        report = engine.run_ofat(
            grid=param_grid,
            port_grid=PORTFOLIO_GRID if not args.quick else _quick_portfolio_grid(),
            progress=_progress,
        )

        ec = build_curve(report.baseline.trades)
        attr = attribution_table(report.baseline.trades)
        rs = [t.r_multiple for t in report.baseline.trades if t.r_multiple is not None]
        boots = bootstrap_all(rs) if len(rs) >= 10 else None
        kel = (kelly_fraction(report.baseline.stats.win_rate,
                              report.baseline.stats.avg_winner_r,
                              abs(report.baseline.stats.avg_loser_r))
               if rs else None)
        stks = consecutive_loss_stats(rs) if rs else None

        print_report(report)

        # ── optional SQL journaling (baseline of the sweep) ───────────────
        if args.journal:
            _journal_baseline(
                report.baseline, report.baseline.trades,
                base_cfg, start_date, end_date, uni,
                notes=f"sweep baseline ({len(report.points)} variants run)",
            )

        if not args.no_csv:
            sp, tp = save_csv(report, out_dir)
            print(f"\n  Saved: {sp}")
            print(f"  Saved: {tp}")
        if not args.no_html:
            hp = save_html(report, out_dir / "backtest_report.html",
                           equity=ec, bootstrap=boots, kelly=kel,
                           attribution=attr, streaks=stks)
            print(f"  Saved: {hp}")

    print(f"\n  Total wall time: {time.time() - t_wall:.1f}s\n")


# ── grid builders ─────────────────────────────────────────────────────────────

def _build_grid(quick: bool) -> list:
    from backtest.sweep import PARAM_GRID, ParamSpec
    if not quick:
        return PARAM_GRID
    reduced = []
    for spec in PARAM_GRID:
        vals = list(spec.values)
        if len(vals) >= 3:
            mid = len(vals) // 2
            vals = [vals[0], vals[mid], vals[-1]]
        reduced.append(ParamSpec(dotted=spec.dotted, values=tuple(vals),
                                 label=spec.label, group=spec.group, fmt=spec.fmt))
    return reduced


def _quick_portfolio_grid() -> list:
    from backtest.sweep import PORTFOLIO_GRID, ParamSpec
    reduced = []
    for spec in PORTFOLIO_GRID:
        vals = list(spec.values)
        if len(vals) > 2:
            vals = [vals[0], vals[-1]]
        reduced.append(ParamSpec(dotted=spec.dotted, values=tuple(vals),
                                 label=spec.label, group=spec.group, fmt=spec.fmt))
    return reduced


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradAlert Backtester",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sweep", action="store_true")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--mean-rev-tune", action="store_true",
                   help="Focused mean-reversion parameter sweep")
    p.add_argument("--walk-forward", action="store_true",
                   help="Rolling 3yr IS / 1yr OOS walk-forward validation")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--tickers", nargs="+", metavar="TICKER")
    p.add_argument("--earnings-aware", action="store_true")
    p.add_argument("--workers", type=int, default=1, metavar="N")
    p.add_argument("--out", default="data/backtest_out", metavar="DIR")
    p.add_argument("--no-html", action="store_true")
    p.add_argument("--no-csv", action="store_true")
    p.add_argument("--journal", action="store_true",
                   help="Write baseline run + trades to MySQL (requires DB env vars)")
    p.add_argument("--log", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level),
                        format="  %(levelname)-8s %(name)s — %(message)s")
    for noisy in ("yfinance", "urllib3", "peewee", "PIL"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)


def _journal_baseline(
        pt,
        trades,
        base_cfg: dict,
        start_date,
        end_date,
        uni,
        notes: str | None = None,
) -> None:
    """
    Write one baseline run + all its closed trades to MySQL.

    Fails gracefully — a DB error prints a warning and does NOT abort the
    backtest process.  Requires DB_HOST / DB_USER / DB_PASSWORD / DB_NAME
    env vars (same as the live scanner).

    Schema: data/backtest_schema.sql (run once before first use).
    """
    try:
        import copy
        from backtest.db import save_backtest_run, save_backtest_trades

        # Attach CLI options so re-readers know the exact run context.
        cfg_snapshot = copy.deepcopy(base_cfg)
        cfg_snapshot["_meta"] = {
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "universe": uni.summary(),
        }

        run_id = save_backtest_run(
            start_date=start_date,
            end_date=end_date,
            tickers_count=uni.n_tradeable,
            stats=pt.stats,
            config=cfg_snapshot,
            notes=notes,
        )
        if run_id is None:
            print("  ⚠  Journal: backtest_runs insert failed — check DB env vars / schema")
            return

        n = save_backtest_trades(run_id, trades)
        print(f"  Journal: run_id={run_id}  {n} trades written to backtest_trades")

    except Exception as exc:
        print(f"  ⚠  Journal skipped — {exc}")


def _die(msg: str) -> None:
    import sys as _sys
    print(f"\n  ✗  {msg}\n", file=_sys.stderr)
    _sys.exit(1)


if __name__ == "__main__":
    main()
