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
    python backtest/run_backtest.py --workers=8             # parallel
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in [str(_ROOT), str(_ROOT / "src")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load secrets.env so DB_* (journaling) and FRED_API_KEY (macro) reach
# os.environ. This entry point must load it explicitly; without it journaling
# fails with "DB env vars not set" even when config/secrets.env is populated.
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


def main() -> None:
    # Force UTF-8 stdout/stderr so the rich console output (─ → ▸ ✓ █) survives
    # piping/redirection on Windows (cp1252), where printing them otherwise
    # raises UnicodeEncodeError. Safe no-op if already UTF-8 or not reconfigurable.
    import sys as _sys
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = _parse_args()
    _setup_logging(args.log)

    t_wall = time.time()

    import yaml
    from backtest.loader import load_universe
    from backtest.report import (
        print_baseline, print_report, save_html, save_csv,
        print_walk_forward, print_mean_rev_tune, print_exit_quality,
    )
    from backtest.sweep import SweepEngine, PORTFOLIO_GRID, MEAN_REV_GRID
    from backtest.equity_curve import build_curve, attribution_table
    from backtest.stats_utils import bootstrap_all, kelly_fraction, consecutive_loss_stats, monte_carlo_drawdown
    from backtest.walk_forward import WalkForwardEngine

    cfg_path = _ROOT / "config" / "filters.yaml"
    wl_path = _ROOT / "config" / "watchlist.yaml"
    if not cfg_path.exists(): _die(f"filters.yaml not found at {cfg_path}")
    if not wl_path.exists():  _die(f"watchlist.yaml not found at {wl_path}")

    with open(cfg_path, encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(wl_path, encoding="utf-8") as f:
        wl_raw = yaml.safe_load(f)

    # Support both legacy flat list and two-tier structure
    if "tier_a" in wl_raw:
        wl_tickers = [t for t in wl_raw["tier_a"] if isinstance(t, str)]
    else:
        wl_tickers = wl_raw.get("tickers", [])

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
        earnings_aware=True,  # always load history; when False the
        # earnings_buffer_days sweep is a no-op (earnings_history stays [],
        # next_earn always None in call_engine_slice)
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
        "max_open_risk": 5.0,  # open-risk budget in size_mult units (~5 full-size
        # positions); risk-adjusted optimum (Sharpe 0.58 @ 5.0 vs 0.55 @ 6.0)
        "earnings_aware": True,  # must match load_universe(earnings_aware=True);
        # run_all() calls _prepare() which respects this flag
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
    }

    # Chronic-loser penalty (--chronic-penalty). Pass the raw config dict through
    # base_port; each sweep worker builds its own TickerHealth so per-run ledgers
    # stay isolated. Flag off → key absent, PortfolioConfig.ticker_health=None.
    if args.chronic_penalty:
        chronic_cfg = base_cfg.get("chronic_loser_penalty", {}) or {}
        # Force enabled even if YAML default is False (the flag is opt-in).
        chronic_cfg = {**chronic_cfg, "enabled": True}
        base_port["chronic_loser_cfg"] = chronic_cfg
        print(f"  ▸ Chronic-loser penalty: ENABLED  "
              f"(lookback={chronic_cfg.get('lookback_days', 90)}d, "
              f"scale={chronic_cfg.get('scale', {2: 0.5, 3: 0.25})})")

    # VIX slope gate (--vix-slope-gate). Mutates base_cfg["regime"] directly
    # because the FilterEngine reads regime.vix_slope_block at signal time.
    if args.vix_slope_gate:
        rcfg = base_cfg.setdefault("regime", {})
        rcfg["vix_slope_block"] = True
        lookback = int(rcfg.get("vix_slope_lookback_days", 5))
        print(f"  ▸ VIX slope gate: ENABLED  (lookback={lookback}d, "
              f"blocks fresh momentum entries when VIX has risen over the window)")

    # Anti-gap entry confirmation (--anti-gap-entry). Mutates base_cfg["signals"]
    # so FilterEngine picks it up via cfg.get("signals", {}).get(...).
    if args.anti_gap_entry:
        scfg = base_cfg.setdefault("signals", {})
        scfg["require_trigger_bar_up"] = True
        print(f"  ▸ Anti-gap entry: ENABLED  "
              f"(blocks T+1 entry when trigger bar closed below its open)")

    # Short trading (--allow-shorts). Mutates base_cfg["signals"] so the
    # FilterEngine emits short entries in BEAR regimes. Off by default → the
    # long-only baseline replays bit-identically.
    if args.allow_shorts:
        scfg = base_cfg.setdefault("signals", {})
        scfg["allow_shorts"] = True
        print("  ▸ Short trading: ENABLED  (signals.allow_shorts=true; "
              "short entries fire in BEAR regimes)")

    # Time-based max-hold exit (--max-hold-days): force-close a held trade at the
    # bar's CLOSE once held N trading bars. Off by default (key absent) so the
    # baseline replays bit-identically. Default from execution.max_hold_days in
    # filters.yaml; the CLI flag overrides it.
    mh_days = exec_cfg.get("max_hold_days")
    mh_mode = str(exec_cfg.get("max_hold_mode", "hard")).replace("-", "_")
    if args.max_hold_days is not None:
        mh_days = args.max_hold_days
    if args.max_hold_mode is not None:
        mh_mode = args.max_hold_mode.replace("-", "_")
    if mh_days is not None:
        base_port["max_hold_days"] = int(mh_days)
        base_port["max_hold_mode"] = mh_mode
        print(f"  ▸ Max-hold exit: ENABLED  ({int(mh_days)} bars, mode={mh_mode}; "
              f"held trades close at the swing horizon — baseline is OFF)")

    # ATR trailing stop. Off by default → baseline identical.
    if args.trail_atr_mult is not None:
        base_port["trail_atr_mult"] = float(args.trail_atr_mult)
        if args.trail_activate_r is not None:
            base_port["trail_activate_r"] = float(args.trail_activate_r)
        print(f"  ▸ ATR trailing stop: ENABLED  (mult={args.trail_atr_mult:g}"
              + (f", activate≥{args.trail_activate_r:g}R" if args.trail_activate_r is not None else "")
              + "; ratchets the stop in the trade's favor — baseline is OFF)")

    # Breakeven stop. `execution.breakeven_trigger_r` in filters.yaml supplies the
    # shipped default (ADR-004); the CLI flag overrides it, and 0 disables.
    be_trigger = exec_cfg.get("breakeven_trigger_r")
    be_buffer = exec_cfg.get("breakeven_buffer_atr")
    be_source = "filters.yaml" if be_trigger else None
    if args.breakeven_trigger_r is not None:
        be_trigger = args.breakeven_trigger_r
        be_source = "CLI"
    if args.breakeven_buffer_atr is not None:
        be_buffer = args.breakeven_buffer_atr
    if be_trigger:  # 0/absent → off
        base_port["breakeven_trigger_r"] = float(be_trigger)
        if be_buffer:
            base_port["breakeven_buffer_atr"] = float(be_buffer)
        print(f"  ▸ Breakeven stop: ENABLED  (trigger≥{float(be_trigger):g}R"
              + (f", buffer={float(be_buffer):g}×ATR" if be_buffer else "")
              + f"; moves stop to breakeven, upside uncapped — {be_source})")
    elif args.breakeven_trigger_r is not None:
        print("  ▸ Breakeven stop: DISABLED (CLI override 0)")

    # Portfolio open-risk budget (--max-open-risk). Default 5.0 (set in base_port
    # above); the flag overrides it for tuning this one-number risk lever.
    if args.max_open_risk is not None:
        base_port["max_open_risk"] = float(args.max_open_risk)
        print(f"  ▸ Open-risk budget: {float(args.max_open_risk):.1f} "
              f"(size_mult units; default 5.0)")

    engine = SweepEngine(
        universe=uni,
        base_cfg=base_cfg,
        base_port_cfg=base_port,
        n_workers=max(args.workers, 0),
    )

    def _progress(msg: str) -> None:
        print(f"  ▸ {msg}", flush=True)

    # ── mean-reversion focused sweep ──────────────────────────────────────
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
    elif not args.sweep and not args.robustness:
        print("\n  Running baseline…", end="", flush=True)
        t0 = time.time()
        pt = engine.baseline()
        print(f" done in {time.time() - t0:.1f}s")

        trades = pt.trades
        ec = build_curve(trades) if trades else None
        rs = [t.effective_r for t in trades if t.exit_date is not None]
        boots = bootstrap_all(rs) if len(rs) >= 10 else None
        kel = (kelly_fraction(pt.stats.win_rate,
                              pt.stats.avg_winner_r,
                              abs(pt.stats.avg_loser_r))
               if rs else None)
        stks = consecutive_loss_stats(rs) if rs else None
        mc_dd = monte_carlo_drawdown(rs) if len(rs) >= 10 else None
        attr = attribution_table(trades) if trades else None

        wf_report = None
        if args.walk_forward:
            re_tune = not args.wf_no_retune
            _wf_workers = max(args.workers, 0)
            if re_tune and args.wf_joint > 0:
                mode_desc = (f"joint re-tune: {args.wf_joint} random "
                             f"{args.wf_joint_knobs}-knob configs per IS window")
            elif re_tune:
                mode_desc = "re-tune sweep per IS window"
            else:
                mode_desc = "fixed-config temporal stability"
            print(f"\n  Running walk-forward validation…  "
                  f"({mode_desc}, workers={_wf_workers})")
            wfe = WalkForwardEngine(
                universe=uni,
                base_cfg=base_cfg,
                base_port_cfg=base_port,
                is_years=3,
                oos_years=1,
                step_months=6,
                re_tune=re_tune,            # --wf-no-retune flips this off (much faster)
                n_workers=_wf_workers,      # --workers now reaches the per-window sweep
                joint_samples=args.wf_joint,
                joint_knobs=args.wf_joint_knobs,
                joint_seed=args.wf_seed,
            )
            wf_report = wfe.run(progress=_progress)

        print_baseline(pt, equity=ec, bootstrap=boots,
                       kelly=kel, attribution=attr, streaks=stks,
                       mc_dd=mc_dd)
        print_exit_quality(trades)  # exit-logic Phase 0 — where do exits leak
        if wf_report:
            print_walk_forward(wf_report)

        # ── SQL journaling (ON by default — policy; --no-journal to skip) ──
        if not args.no_journal:
            _journal_baseline(pt, trades, base_cfg, start_date, end_date, uni)
        else:
            print("  ▸ Journaling: OFF (--no-journal) — this run leaves no DB record")

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

    # ── robustness sweep ───────────────────────────────────────────────────
    elif args.robustness:
        from backtest.report import print_robustness
        from backtest.sweep import PARAM_GRID, _set_nested

        print(f"\n  Robustness sweep: perturb each param ±10%, ±20%\n")
        bl = engine.baseline()
        base_er = bl.stats.expectancy_r

        robust_results = []
        for spec in PARAM_GRID:
            baseline_val = engine._resolve_baseline(spec)
            if baseline_val is None or not isinstance(baseline_val, (int, float)):
                continue
            if baseline_val == 0:
                continue

            perturbations = {}
            for pct in [-0.20, -0.10, +0.10, +0.20]:
                perturbed_val = baseline_val * (1 + pct)
                if spec.fmt == "{:.0f}":
                    perturbed_val = round(perturbed_val)
                perturbations[pct] = perturbed_val

            for pct, val in perturbations.items():
                cfg = copy.deepcopy(base_cfg)
                port_params = dict(base_port)
                _set_nested(cfg, spec.dotted, val)

                pt = engine._run_one(
                    cfg=cfg,
                    port_params=port_params,
                    param_name=spec.dotted,
                    param_value=val,
                    param_label=spec.label,
                    group=spec.group,
                    is_baseline=False,
                )
                robust_results.append({
                    "param": spec.label,
                    "group": spec.group,
                    "baseline": baseline_val,
                    "pct": pct,
                    "value": val,
                    "er": pt.stats.expectancy_r,
                    "trades": pt.stats.trades_count,
                })

        print_robustness(robust_results, base_er)

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
        rs = [t.effective_r for t in report.baseline.trades if t.exit_date is not None]
        boots = bootstrap_all(rs) if len(rs) >= 10 else None
        kel = (kelly_fraction(report.baseline.stats.win_rate,
                              report.baseline.stats.avg_winner_r,
                              abs(report.baseline.stats.avg_loser_r))
               if rs else None)
        stks = consecutive_loss_stats(rs) if rs else None

        print_report(report)

        # ── SQL journaling of the sweep baseline (ON by default — policy) ──
        if not args.no_journal:
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
    p.add_argument("--sweep", action="store_true",
                   help="Run the full OFAT parameter sweep (one factor at a "
                        "time over backtest/sweep.py PARAM_GRID).")
    p.add_argument("--quick", action="store_true",
                   help="With --sweep: use the reduced grid (fewer values per "
                        "parameter) for a fast pass.")
    p.add_argument("--mean-rev-tune", action="store_true",
                   help="Focused mean-reversion parameter sweep")
    p.add_argument("--walk-forward", action="store_true",
                   help="Rolling 3yr IS / 1yr OOS walk-forward validation")
    p.add_argument("--wf-joint", type=int, default=0, metavar="N",
                   help="With --walk-forward re-tune: replace the per-window OFAT "
                        "sweep with N randomized multi-knob configs (seeded, "
                        "reproducible). OFAT mutates one knob per config and so "
                        "understates multi-parameter overfitting; joint sampling "
                        "reproduces a multi-knob selection with an explicit trial "
                        "count per window. 0 = keep OFAT. NOTE: degradation is "
                        "only comparable across runs with the same mode and N "
                        "(OFAT runs ~90 trials/window; pick N accordingly).")
    p.add_argument("--wf-joint-knobs", type=int, default=3, metavar="K",
                   help="With --wf-joint: knobs mutated per sampled config "
                        "(default 3).")
    p.add_argument("--wf-seed", type=int, default=1337, metavar="S",
                   help="Seed for the --wf-joint sampler (offset per window; the "
                        "same seed reproduces the same candidate sets). The seed "
                        "is printed in the report tag — re-running with several "
                        "seeds and quoting the prettiest degradation reintroduces "
                        "the selection bias this mode exists to measure.")
    p.add_argument("--wf-no-retune", action="store_true",
                   help="With --walk-forward: skip the per-window re-tune sweep and "
                        "run the FIXED current config on each IS/OOS window "
                        "(temporal-stability test). ~18 runs vs ~900 — far faster, "
                        "and the right test for 'does the shipped config survive OOS'. "
                        "Re-tune (the default) tests whether parameter selection "
                        "generalises and is much slower; use --workers N to parallelise it.")
    p.add_argument("--robustness", action="store_true",
                   help="Perturb each param plus/minus 10pct/20pct and report E[R] sensitivity")
    p.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                   help="First in-window entry date (inclusive). "
                        "Default: earliest available bar.")
    p.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                   help="Last in-window entry date (inclusive). "
                        "Default: latest available bar.")
    p.add_argument("--tickers", nargs="+", metavar="TICKER",
                   help="Restrict the run to these tickers (space-separated). "
                        "Default: the full watchlist universe.")
    p.add_argument("--workers", type=int, default=1, metavar="N",
                   help="Parallel worker processes for sweep / walk-forward "
                        "(1 = sequential).")
    p.add_argument("--out", default="data/backtest_out", metavar="DIR",
                   help="Output directory for the HTML report and CSV ledger.")
    p.add_argument("--no-html", action="store_true",
                   help="Skip writing the HTML report.")
    p.add_argument("--no-csv", action="store_true",
                   help="Skip writing the CSV trade ledger (trades.csv).")
    p.add_argument("--journal", action="store_true",
                   help="(deprecated — journaling is ON by default) kept for compatibility.")
    p.add_argument("--no-journal", action="store_true",
                   help="Skip MySQL journaling for this run (default: journal it, so every "
                        "run leaves data for the live-reconciliation feed).")
    p.add_argument("--chronic-penalty", action="store_true",
                   help="Enable per-ticker chronic-loser size penalty "
                        "(see config/filters.yaml `chronic_loser_penalty`). "
                        "Off by default so baseline replays identically.")
    p.add_argument("--vix-slope-gate", action="store_true",
                   help="Enable VIX slope gate: block fresh momentum entries "
                        "when VIX has risen over the configured lookback window "
                        "(see config/filters.yaml `regime.vix_slope_block`). "
                        "Off by default so baseline replays identically.")
    p.add_argument("--anti-gap-entry", action="store_true",
                   help="Require trigger bar close >= open before queuing the "
                        "T+1 entry (see config/filters.yaml "
                        "`signals.require_trigger_bar_up`). Off by default.")
    p.add_argument("--allow-shorts", action="store_true",
                   help="Enable short-side entries: sets "
                        "signals.allow_shorts=true so the engine fires shorts "
                        "in BEAR regimes. Off by default so the long-only "
                        "baseline replays identically.")
    p.add_argument("--max-hold-days", type=int, default=None, metavar="N",
                   help="Enforce a swing-trading horizon: force-close a held "
                        "trade at the bar's close once it has been held N "
                        "trading bars (exit reason 'time_stop'). Off by "
                        "default so the baseline replays identically. "
                        "Default source: execution.max_hold_days in filters.yaml.")
    p.add_argument("--max-hold-mode", default=None,
                   choices=["hard", "if-not-profit"],
                   help="With --max-hold-days: 'hard' (default) always exits "
                        "at the cap; 'if-not-profit' exits at the cap only when "
                        "the position is not in profit (lets winners run to "
                        "target).")
    p.add_argument("--trail-atr-mult", type=float, default=None, metavar="M",
                   help="ATR trailing stop: ratchet the stop to "
                        "highest_high − ATR×M (long; short mirrors), in the trade's "
                        "favor only. Off by default so the baseline replays "
                        "identically. R stays off the INITIAL stop — the trail changes "
                        "only the exit price/reason.")
    p.add_argument("--trail-activate-r", type=float, default=None, metavar="R",
                   help="With --trail-atr-mult: only start trailing once the trade has "
                        "reached this MFE in R (default: trail from entry).")
    p.add_argument("--breakeven-trigger-r", type=float, default=None, metavar="R",
                   help="Breakeven stop: once the trade reaches this MFE in R, move "
                        "the stop to breakeven — protects the downside WITHOUT "
                        "capping the upside (does not trail further). Default comes "
                        "from execution.breakeven_trigger_r in filters.yaml "
                        "(shipped: 1.0, ADR-004); pass 0 to disable. R stays off "
                        "the INITIAL stop.")
    p.add_argument("--breakeven-buffer-atr", type=float, default=None, metavar="M",
                   help="With --breakeven-trigger-r: place the breakeven stop M×ATR in "
                        "profit past entry (default 0 = exact breakeven).")
    p.add_argument("--max-open-risk", type=float, default=None, metavar="R",
                   help="Aggregate open-risk budget in size_mult units (default "
                        "5.0). Each open position consumes its own size_mult, so a "
                        "new entry is dropped once total open risk would exceed "
                        "this budget. Lower → fewer concurrent positions.")
    p.add_argument("--log", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Console log verbosity. Default: WARNING.")
    return p.parse_args()


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level),
                        format="  %(levelname)-8s %(name)s — %(message)s")
    # Defensive: mask anything resembling an API key (e.g. a leaked FRED key in
    # a URL) before it reaches the log, same as main.py. The filter lives on the
    # handlers — not the root logger — so it also catches records propagated up
    # from child loggers.
    from core.fetchers.http import mask_api_keys_filter
    _mask = mask_api_keys_filter()
    for _h in logging.getLogger().handlers:
        _h.addFilter(_mask)
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
            # backtest.db.reference_run selects the expectancy reference on
            # `use_scoring is False`. All runs are scoring-OFF now; writing the
            # constant keeps older scoring-ON rows distinguishable (and skipped).
            "use_scoring": False,
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
        # Differentiate "DB not configured" from real errors so the operator
        # knows whether to fix env vars or schema.
        msg = str(exc) or type(exc).__name__
        if "environment variable" in msg or "DB_" in msg:
            print(f"  ⚠  Journal skipped — DB env vars not set ({msg})")
        else:
            print(f"  ⚠  Journal skipped — {msg}")


def _die(msg: str) -> None:
    import sys as _sys
    print(f"\n  ✗  {msg}\n", file=_sys.stderr)
    _sys.exit(1)


if __name__ == "__main__":
    main()
