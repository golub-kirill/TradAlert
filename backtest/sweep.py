"""
OFAT Parameter Sweep Engine for TradAlert backtests.

One-Factor-At-a-Time (OFAT) design
────────────────────────────────────
Every parameter is swept independently against a fixed baseline config.
This avoids the combinatorial explosion of a full grid (e.g. 10 params × 4
values = 4^10 = 1M runs) while still surfacing the direction and magnitude
of each parameter's effect on performance.

Architecture
────────────
    1.  UniverseData  — loaded ONCE by the caller (loader.load_universe).
        All sweep runs reuse the same pre-loaded OHLCV + indicators.

    2.  SweepEngine   — accepts the universe and base config, exposes
        .baseline() and .run_ofat() methods.

    3.  _run_one()    — inner hot-path: mutates a config copy, constructs
        FilterEngine.from_dict(), runs PortfolioBacktester.run_prepped(),
        collects Stats + group breakdowns.

    4.  Parallel execution via ProcessPoolExecutor.  Each worker receives
        the config dict + portfolio params + pickled prepped data.
        DataFrames serialise efficiently via pickle (numpy arrays).

Public API
──────────
    SweepEngine.baseline()          -> SweepPoint
    SweepEngine.run_ofat(grid)      -> SweepReport

    PARAM_GRID                      : default OFAT spec (18 parameters)
    PORTFOLIO_GRID                  : portfolio-level sweep spec
"""

from __future__ import annotations

import copy
import logging
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from backtest.loader import UniverseData
from backtest.stats import Stats, compute_stats, group_by
from backtest.trade import Trade

logger = logging.getLogger(__name__)


# ── parameter specification ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ParamSpec:
    """
    One sweepable dimension.

    Attributes
    ----------
    dotted    : Dot-path into the FilterEngine config dict, e.g.
                ``"signals.stop_loss.atr_multiplier"``.
                Use ``"portfolio.<field>"`` for PortfolioConfig params.
    values    : Ordered list of values to test (baseline value included).
    label     : Human-readable parameter name for reports.
    group     : Category for grouped display (e.g. ``"stop_loss"``).
    fmt       : Python format string for value display. Default ``"{:.2f}"``.
    """
    dotted: str
    values: tuple
    label: str
    group: str
    fmt: str = "{:.3g}"


# ── default sweep grid ────────────────────────────────────────────────────────

PARAM_GRID: list[ParamSpec] = [
    # Stop / Target structure
    ParamSpec("signals.stop_loss.atr_multiplier",
              (1.0, 1.5, 2.0, 2.5, 3.0),
              "ATR stop multiplier", "stop_loss"),
    ParamSpec("signals.stop_loss.min_rr",
              (1.5, 2.0, 2.5, 3.0, 4.0),
              "Min risk:reward", "stop_loss"),

    # Momentum long entry
    ParamSpec("signals.momentum.long.rsi_min",
              (35, 40, 45, 50, 55),
              "Momentum RSI floor", "momentum_entry"),
    ParamSpec("signals.momentum.long.rsi_max",
              (55, 60, 65, 70, 75),
              "Momentum RSI ceiling", "momentum_entry"),
    ParamSpec("signals.momentum.long.min_hist_delta_atr",
              (0.02, 0.05, 0.08, 0.12, 0.18),
              "Momentum MACD delta gate", "momentum_entry"),

    # Momentum exit (fade trigger)
    ParamSpec("signals.momentum.short.rsi_min",
              (25, 30, 35, 40),
              "Momentum fade RSI floor", "momentum_exit"),
    ParamSpec("signals.momentum.short.rsi_max",
              (40, 45, 50, 55),
              "Momentum fade RSI ceiling", "momentum_exit"),

    # Mean-reversion long entry
    ParamSpec("signals.mean_reversion.long.rsi_max",
              (25, 30, 35, 40, 45),
              "Mean-rev RSI ceiling", "mean_reversion"),
    ParamSpec("signals.mean_reversion.long.min_hist_delta_atr",
              (0.02, 0.05, 0.08, 0.12),
              "Mean-rev MACD delta gate", "mean_reversion"),

    # Mean-reversion exit trigger
    ParamSpec("signals.mean_reversion.short.rsi_min",
              (55, 60, 65, 70),
              "Mean-rev exit RSI floor", "mean_reversion"),

    # Regime
    ParamSpec("regime.vix_low",
              (15, 18, 20, 22),
              "VIX low threshold", "regime"),
    ParamSpec("regime.vix_high",
              (20, 23, 25, 28, 30),
              "VIX high threshold", "regime"),
    ParamSpec("regime.require_ma_short_alignment",
              (False, True),
              "Require MA20 alignment", "regime"),
    ParamSpec("signals.momentum.long.max_bars_since_cross",
              (1, 2, 3, 5, 8),
              "Max bars since MACD cross", "momentum_entry",
              fmt="{:.0f}"),
    ParamSpec("signals.gap_risk.max_prev_bar_range_atr",
              (1.5, 2.0, 2.5, 3.0, 4.0),
              "Max prev bar range (ATR)", "gap_risk",
              fmt="{:.1f}"),

    # Events
    ParamSpec("events.earnings_buffer_days",
              (2, 3, 5, 7, 10),
              "Earnings buffer days", "events",
              fmt="{:.0f}"),

    # Phase 1 — 52-week proximity
    ParamSpec("scanner.weights.near_52w_high",
              (0, 1, 2, 3, 4),
              "Near 52w high weight", "phase1"),
    ParamSpec("scanner.weights.far_from_52w_low",
              (0, 1, 2, 3, 4),
              "Far from 52w low weight", "phase1"),

    # Phase 2 — RP percentile
    ParamSpec("scanner.weights.rp_percentile",
              (0, 1, 2, 3, 4, 5),
              "RP percentile weight", "phase2"),

    # Phase 3 — MA200 slope
    ParamSpec("scanner.weights.ma200_slope",
              (0, 1, 2),
              "MA200 slope weight", "phase3"),

    # Phase 4 — VBP exit
    ParamSpec("scanner.exit_weights.vbp_resistance",
              (0, 1, 2, 3),
              "VBP resistance weight", "phase4"),

    # Global
    ParamSpec("scanner.min_score_to_alert",
              (55, 60, 65, 70, 75),
              "Min score to alert", "global",
              fmt="{:.0f}"),

    # Phase 8 — Behavioral breadth divergence penalty
    ParamSpec("behavioral.breadth_divergence_penalty",
              (0.0, 0.1, 0.2, 0.3),
              "Breadth divergence pen.", "phase8"),

    # Phase 8 — Behavioral size multiplier floor
    ParamSpec("behavioral.size_multiplier_floor",
              (0.25, 0.35, 0.50, 0.65),
              "Behavioral size floor", "phase8"),
]

PORTFOLIO_GRID: list[ParamSpec] = [
    ParamSpec("portfolio.max_concurrent",
              (2, 3, 5, 8, 10),
              "Max concurrent positions", "portfolio",
              fmt="{:.0f}"),
    ParamSpec("portfolio.entry_slippage_pct",
              (0.0, 0.0005, 0.001, 0.002),
              "Entry slippage %", "portfolio",
              fmt="{:.4f}"),
    ParamSpec("portfolio.commission_r",
              (0.0, 0.003, 0.005, 0.010),
              "Commission (R units)", "portfolio",
              fmt="{:.4f}"),
    ParamSpec("portfolio.max_drawdown_r",
              (6.0, 8.0, 10.0, 12.0),
              "Max portfolio drawdown (R)", "portfolio",
              fmt="{:.1f}"),
]

# ── mean-reversion focused grid ───────────────────────────────────────────────
# Used by --mean-rev-tune to diagnose / fix the mean-reversion signal.
# Tests wide parameter ranges and includes a "disabled" sentinel value.

MEAN_REV_GRID: list[ParamSpec] = [
    # Entry RSI ceiling -- how oversold must price be to enter?
    ParamSpec("signals.mean_reversion.long.rsi_max",
              (20, 25, 30, 35, 40, 45),
              "MR entry RSI ceiling", "mean_rev_entry"),

    # MACD histogram delta gate -- confirmation of momentum turning up
    ParamSpec("signals.mean_reversion.long.min_hist_delta_atr",
              (0.01, 0.03, 0.05, 0.08, 0.12, 0.18),
              "MR MACD delta gate", "mean_rev_entry"),

    # Exit RSI floor -- when to take profit on a mean-rev long
    ParamSpec("signals.mean_reversion.short.rsi_min",
              (50, 55, 60, 65, 70, 75),
              "MR exit RSI floor", "mean_rev_exit"),
]

# ── scoring system focused grid ───────────────────────────────────────────────
# Used by --scoring-sweep to tune the SignalScorer: entry/exit thresholds
# and sub-score weights that gate trade selection via min_score_to_alert.
# All paths map to settings.yaml via _SETTINGS_ALIASES.

SCORING_GRID: list[ParamSpec] = [
    # ── Entry thresholds (shape the scoring curves) ───────────────────────
    ParamSpec("scanner.entry_thresholds.rsi_healthy_center",
              (45, 50, 52.5, 55, 60),
              "RSI healthy center", "scoring_entry", fmt="{:.1f}"),
    ParamSpec("scanner.entry_thresholds.rsi_healthy_half_w",
              (8, 10, 12.5, 15, 20),
              "RSI healthy half-width", "scoring_entry", fmt="{:.1f}"),
    ParamSpec("scanner.entry_thresholds.ma50_slope_scale",
              (1.0, 1.5, 2.0, 3.0, 4.0),
              "MA50 slope scale", "scoring_entry", fmt="{:.1f}"),
    ParamSpec("scanner.entry_thresholds.breakout_band_pct",
              (1.5, 2.0, 3.0, 4.0, 5.0),
              "Breakout band %", "scoring_entry", fmt="{:.1f}"),
    ParamSpec("scanner.entry_thresholds.volume_spike_ratio",
              (1.5, 2.0, 2.5, 3.0),
              "Volume spike ratio", "scoring_entry", fmt="{:.1f}"),
    ParamSpec("scanner.entry_thresholds.near_52w_high_pct_band",
              (15, 20, 25, 30, 35),
              "52w high band %", "scoring_entry", fmt="{:.0f}"),
    ParamSpec("scanner.entry_thresholds.far_from_52w_low_pct_floor",
              (20, 25, 30, 35, 40),
              "52w low floor %", "scoring_entry", fmt="{:.0f}"),

    # ── Entry weights (relative importance of sub-scores) ──────────────────
    ParamSpec("scanner.weights.trend_up",
              (1, 2, 3, 4, 5),
              "Trend-up weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.ma50_slope",
              (1, 2, 3, 4),
              "MA50 slope weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.volume_spike",
              (1, 2, 3, 4),
              "Volume spike weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.rsi_healthy",
              (1, 2, 3, 4),
              "RSI healthy weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.breakout_20d",
              (1, 2, 3, 4, 5),
              "Breakout 20d weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.macd_bullish",
              (1, 2, 3, 4, 5),
              "MACD bullish weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.relative_strength",
              (0, 1, 2, 3, 4),
              "Relative strength weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.weekly_trend",
              (0, 1, 2, 3, 4),
              "Weekly trend weight", "scoring_weights", fmt="{:.0f}"),
    ParamSpec("scanner.weights.bb_zscore",
              (0, 1, 2, 3, 4),
              "BB Z-score weight", "scoring_weights", fmt="{:.0f}"),

    # ── Exit thresholds ────────────────────────────────────────────────────
    ParamSpec("scanner.exit_thresholds.rsi_overbought_floor",
              (55, 60, 65, 70),
              "RSI overbought floor", "scoring_exit", fmt="{:.0f}"),
    ParamSpec("scanner.exit_thresholds.rsi_overbought_range",
              (5, 8, 10, 15, 20),
              "RSI overbought range", "scoring_exit", fmt="{:.0f}"),
    ParamSpec("scanner.exit_thresholds.multi_bar_decay_max",
              (2, 3, 4, 5),
              "Multi-bar decay max", "scoring_exit", fmt="{:.0f}"),
    ParamSpec("scanner.exit_thresholds.vol_expansion_ratio",
              (0.3, 0.5, 0.8, 1.0, 1.5),
              "Vol expansion ratio", "scoring_exit", fmt="{:.1f}"),

    # ─ Exit weights ───────────────────────────────────────────────────────
    ParamSpec("scanner.exit_weights.regime_flip",
              (2, 3, 4, 5, 6),
              "Regime flip weight", "scoring_exit_weights", fmt="{:.0f}"),
    ParamSpec("scanner.exit_weights.multi_bar_decay",
              (1, 2, 3, 4, 5),
              "Multi-bar decay weight", "scoring_exit_weights", fmt="{:.0f}"),
    ParamSpec("scanner.exit_weights.rsi_overbought",
              (1, 2, 3, 4),
              "RSI overbought weight", "scoring_exit_weights", fmt="{:.0f}"),
    ParamSpec("scanner.exit_weights.macd_cross_down",
              (1, 2, 3, 4, 5),
              "MACD cross-down weight", "scoring_exit_weights", fmt="{:.0f}"),
    ParamSpec("scanner.exit_weights.vol_expansion",
              (1, 2, 3, 4),
              "Vol expansion weight", "scoring_exit_weights", fmt="{:.0f}"),
    ParamSpec("scanner.exit_weights.rs_divergence",
              (0, 1, 2, 3, 4),
              "RS divergence weight", "scoring_exit_weights", fmt="{:.0f}"),
]


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class SweepPoint:
    """Results for one (parameter, value) combination."""
    run_id: str
    param_name: str
    param_value: Any
    param_label: str
    group: str
    is_baseline: bool

    # Aggregate
    stats: Stats
    # Breakdowns
    by_signal: dict[str, Stats] = field(default_factory=dict)
    by_regime: dict[str, Stats] = field(default_factory=dict)
    by_exit: dict[str, Stats] = field(default_factory=dict)
    by_year: dict[str, Stats] = field(default_factory=dict)

    n_tickers: int = 0
    elapsed_s: float = 0.0
    trades: list[Trade] = field(default_factory=list)

    # ── helpers ───────────────────────────────────────────────────────────

    def fmt_value(self, spec: ParamSpec | None = None) -> str:
        if spec:
            return spec.fmt.format(self.param_value)
        try:
            return f"{self.param_value:.3g}"
        except (TypeError, ValueError):
            return str(self.param_value)

    @property
    def label_with_value(self) -> str:
        suffix = " * base" if self.is_baseline else ""
        return f"{self.param_value}{suffix}"


@dataclass
class SweepReport:
    """Full OFAT sweep output."""
    baseline: SweepPoint
    points: list[SweepPoint]
    universe_info: str
    elapsed_s: float
    n_workers: int

    # ── convenience accessors ─────────────────────────────────────────────

    @property
    def all_points(self) -> list[SweepPoint]:
        """Baseline + all sweep points."""
        return [self.baseline] + self.points

    def by_group(self) -> dict[str, list[SweepPoint]]:
        """Points grouped by param group, baseline excluded."""
        out: dict[str, list[SweepPoint]] = {}
        for p in self.points:
            out.setdefault(p.group, []).append(p)
        return out

    def best(self, metric: str = "expectancy_r", n: int = 5) -> list[SweepPoint]:
        """Top-N non-baseline points by a Stats attribute."""
        ranked = sorted(
            self.points,
            key=lambda p: getattr(p.stats, metric, 0.0),
            reverse=True,
        )
        return ranked[:n]

    def sensitivity(self) -> pd.DataFrame:
        """
        Per-parameter sensitivity table.

        Each row = one ParamSpec, columns = best / worst / baseline / spread
        for expectancy_r.  Useful for identifying which params matter most.
        """
        rows = []
        for group_name, pts in self.by_group().items():
            param_label = pts[0].param_label if pts else group_name
            ers = [p.stats.expectancy_r for p in pts]
            wrs = [p.stats.win_rate for p in pts]
            rows.append({
                "group": group_name,
                "param": param_label,
                "n_values": len(pts),
                "er_best": max(ers),
                "er_worst": min(ers),
                "er_spread": max(ers) - min(ers),
                "er_baseline": self.baseline.stats.expectancy_r,
                "wr_best": max(wrs),
                "wr_worst": min(wrs),
            })
        df = pd.DataFrame(rows)
        if df.empty or "er_spread" not in df.columns:
            return df
        return df.sort_values("er_spread", ascending=False).reset_index(drop=True)

    def to_dataframe(self) -> pd.DataFrame:
        """Flat DataFrame with one row per SweepPoint (all stats + labels)."""
        rows = []
        for p in self.all_points:
            s = p.stats
            rows.append({
                "run_id": p.run_id,
                "group": p.group,
                "param": p.param_label,
                "value": p.param_value,
                "is_baseline": p.is_baseline,
                "trades": s.trades_count,
                "win_rate": round(s.win_rate, 4),
                "expectancy_r": round(s.expectancy_r, 4),
                "total_r": round(s.total_r, 4),
                "profit_factor": round(min(s.profit_factor, 999.0), 4),
                "max_drawdown_r": round(s.max_drawdown_r, 4),
                "best_trade_r": round(s.best_trade_r, 4),
                "worst_trade_r": round(s.worst_trade_r, 4),
                "avg_bars_held": round(s.avg_bars_held, 1),
                "wins": s.wins,
                "losses": s.losses,
                "elapsed_s": round(p.elapsed_s, 2),
            })
        return pd.DataFrame(rows)

    def trades_dataframe(self) -> pd.DataFrame:
        """All trades from the baseline run as a flat DataFrame."""
        trades = self.baseline.trades
        if not trades:
            return pd.DataFrame()
        return pd.DataFrame([
            {
                "ticker": t.ticker,
                "signal_type": t.signal_type,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "initial_stop": t.initial_stop,
                "initial_target": t.initial_target,
                "exit_reason": t.exit_reason,
                "r_multiple": round(t.r_multiple, 4),
                "bars_held": t.bars_held,
                "market_regime": t.market_regime,
                "ticker_trend": t.ticker_trend,
                "year": t.entry_date.year if t.entry_date else None,
            }
            for t in sorted(trades, key=lambda x: x.entry_date or date.min)
        ])


# ── sweep engine ──────────────────────────────────────────────────────────────

class SweepEngine:
    """
    OFAT sweep over a pre-loaded universe.

    Parameters
    ----------
    universe       : UniverseData from loader.load_universe().
    base_cfg       : Full filters.yaml config dict (base for all mutations).
    base_port_cfg  : Baseline PortfolioConfig keyword arguments.
    n_workers      : Parallel worker processes. 0 = sequential.
                     Defaults to min(cpu_count, 6) for safety.
    """

    def __init__(
            self,
            universe: UniverseData,
            base_cfg: dict,
            base_port_cfg: dict | None = None,
            n_workers: int | None = None,
    ) -> None:
        self._universe = universe
        self._base_cfg = base_cfg
        self._base_port = base_port_cfg or {
            "max_concurrent": 5,
            "earnings_aware": False,
            "entry_slippage_pct": 0.001,
            "commission_r": 0.005,
            "close_open_at_eod": True,
        }
        cpu = multiprocessing.cpu_count()
        self._n_workers = n_workers if n_workers is not None else min(cpu, 6)
        logger.info(
            "SweepEngine ready -- %d workers | %s",
            self._n_workers, universe.summary(),
        )

    # ── public API ────────────────────────────────────────────────────────

    def baseline(self) -> SweepPoint:
        """Run the unmodified base config and return its SweepPoint."""
        return self._run_one(
            cfg=copy.deepcopy(self._base_cfg),
            port_params=self._base_port,
            param_name="baseline",
            param_value="baseline",
            param_label="Baseline",
            group="baseline",
            is_baseline=True,
        )

    def run_ofat(
            self,
            grid: list[ParamSpec] | None = None,
            port_grid: list[ParamSpec] | None = None,
            progress: Callable[[str], None] | None = None,
    ) -> SweepReport:
        """
        Run OFAT sweep over all parameter specs.

        Parameters
        ----------
        grid      : Filter-engine param specs. Defaults to PARAM_GRID.
        port_grid : Portfolio param specs. Defaults to PORTFOLIO_GRID.
        progress  : Optional callback(message) called before each run.

        Returns
        -------
        SweepReport with baseline + all sweep points.
        """
        grid = grid if grid is not None else PARAM_GRID
        port_grid = port_grid if port_grid is not None else PORTFOLIO_GRID
        all_specs = list(grid) + list(port_grid)

        t_total = time.time()

        # ── baseline first (always sequential) ───────────────────────────
        if progress:
            progress("Running baseline...")
        base_pt = self.baseline()
        logger.info("Baseline: %d trades E[R]=%+.3f",
                    base_pt.stats.trades_count, base_pt.stats.expectancy_r)

        # ── build job list ────────────────────────────────────────────────
        jobs: list[dict] = []
        for spec in all_specs:
            is_portfolio = spec.dotted.startswith("portfolio.")
            baseline_val = self._resolve_baseline(spec)
            for val in spec.values:
                if val == baseline_val:
                    continue  # skip exact baseline -- already have it
                jobs.append({
                    "spec": spec,
                    "val": val,
                    "is_portfolio": is_portfolio,
                    "baseline_val": baseline_val,
                })

        logger.info("Sweep: %d jobs, %d workers", len(jobs), self._n_workers)

        # ── execute ───────────────────────────────────────────────────────
        points: list[SweepPoint] = []

        if self._n_workers <= 1:
            for job in jobs:
                if progress:
                    spec = job["spec"]
                    progress(f"{spec.label} = {spec.fmt.format(job['val'])}")
                pt = self._dispatch_job(job)
                points.append(pt)
        else:
            points = self._run_parallel(jobs, progress)

        elapsed = time.time() - t_total
        logger.info("Sweep complete in %.1fs -- %d points", elapsed, len(points))

        return SweepReport(
            baseline=base_pt,
            points=points,
            universe_info=self._universe.summary(),
            elapsed_s=elapsed,
            n_workers=self._n_workers,
        )

    # ── private ───────────────────────────────────────────────────────────

    def _resolve_baseline(self, spec: ParamSpec) -> Any:
        """Extract the baseline value for a ParamSpec from the live configs."""
        if spec.dotted.startswith("portfolio."):
            key = spec.dotted[len("portfolio."):]
            return self._base_port.get(key)
        parts = spec.dotted.split(".")
        node = self._base_cfg
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def _dispatch_job(self, job: dict) -> SweepPoint:
        """Build args and call _run_one for a single job dict."""
        spec = job["spec"]
        val = job["val"]
        is_portfolio = job["is_portfolio"]

        cfg = copy.deepcopy(self._base_cfg)
        port_params = dict(self._base_port)

        if is_portfolio:
            key = spec.dotted[len("portfolio."):]
            port_params[key] = val
        else:
            _set_nested(cfg, spec.dotted, val)

        return self._run_one(
            cfg=cfg,
            port_params=port_params,
            param_name=spec.dotted,
            param_value=val,
            param_label=spec.label,
            group=spec.group,
            is_baseline=False,
        )

    def _run_parallel(
            self,
            jobs: list[dict],
            progress: Callable | None,
    ) -> list[SweepPoint]:
        """Execute jobs across a ProcessPoolExecutor."""
        packed_universe = _pack_universe(self._universe)
        base_cfg_copy = copy.deepcopy(self._base_cfg)
        base_port_copy = dict(self._base_port)

        futures = {}
        points: list[SweepPoint] = [None] * len(jobs)  # preserve order
        src_path = os.path.join(os.path.dirname(__file__), "..")

        with ProcessPoolExecutor(
                max_workers=self._n_workers,
                initializer=_worker_init,
                initargs=(src_path,),
        ) as pool:
            for i, job in enumerate(jobs):
                spec = job["spec"]
                val = job["val"]
                is_portfolio = job["is_portfolio"]

                cfg = copy.deepcopy(base_cfg_copy)
                port_params = dict(base_port_copy)

                if is_portfolio:
                    key = spec.dotted[len("portfolio."):]
                    port_params[key] = val
                else:
                    _set_nested(cfg, spec.dotted, val)

                fut = pool.submit(
                    _worker_run,
                    packed_universe,
                    cfg,
                    port_params,
                    spec.dotted,
                    val,
                    spec.label,
                    spec.group,
                )
                futures[fut] = i

            n_done = 0
            for fut in as_completed(futures):
                idx = futures[fut]
                job = jobs[idx]
                spec = job["spec"]
                try:
                    pt = fut.result()
                    points[idx] = pt
                except Exception as exc:
                    logger.error("Job failed [%s=%s]: %s",
                                 spec.label, job["val"], exc)
                    points[idx] = _empty_point(
                        spec.dotted, job["val"], spec.label, spec.group
                    )
                n_done += 1
                if progress:
                    progress(
                        f"[{n_done}/{len(jobs)}] "
                        f"{spec.label} = {spec.fmt.format(job['val'])}"
                    )

        return [p for p in points if p is not None]

    def _run_one(
            self,
            cfg: dict,
            port_params: dict,
            param_name: str,
            param_value: Any,
            param_label: str,
            group: str,
            is_baseline: bool,
    ) -> SweepPoint:
        """Hot-path: construct engine, run backtest, collect stats."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

        from core.filter_engine import FilterEngine
        from core.scoring import SignalScorer
        from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig

        run_id = f"{group}/{param_name}={param_value}"
        t0 = time.time()

        try:
            engine = FilterEngine.from_dict(cfg)
        except Exception as exc:
            logger.warning("FilterEngine.from_dict failed [%s]: %s", run_id, exc)
            return _empty_point(param_name, param_value, param_label, group, is_baseline)

        _PORT_FIELDS = {
            "max_concurrent", "start_date", "end_date", "earnings_aware",
            "close_open_at_eod", "entry_slippage_pct", "commission_r",
        }
        pcfg_kwargs = {k: v for k, v in port_params.items() if k in _PORT_FIELDS}
        try:
            pcfg = PortfolioConfig(**pcfg_kwargs)
        except Exception as exc:
            logger.warning("PortfolioConfig failed [%s]: %s", run_id, exc)
            return _empty_point(param_name, param_value, param_label, group, is_baseline)

        # Wire scorer so min_score_to_alert gate is applied in backtesting
        scorer = None
        _settings = None
        try:
            import yaml as _yaml
            _settings_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
            with open(_settings_path, encoding="utf-8") as _f:
                _settings = _yaml.safe_load(_f)

            # Mutate _settings for sweep params that live in settings.yaml
            # (scanner weights, exit_weights, min_score_to_alert, behavioral params)
            if not is_baseline:
                _apply_settings_mutation(_settings, param_name, param_value)

            scorer = SignalScorer(_settings, cfg)
        except Exception as exc:
            logger.debug("SignalScorer init failed — running without score gate: %s", exc)
            # Still load settings for behavioral classification even if scorer fails
            if _settings is None:
                try:
                    import yaml as _yaml
                    _settings_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
                    with open(_settings_path, encoding="utf-8") as _f:
                        _settings = _yaml.safe_load(_f)
                    if not is_baseline:
                        _apply_settings_mutation(_settings, param_name, param_value)
                except Exception:
                    pass

        bt = PortfolioBacktester(engine, pcfg, scorer=scorer)
        result = bt.run_prepped(
            self._universe.prepped,
            self._universe.skipped,
            self._universe.market_dfs,
            self._universe.vix_df,
            macro_series=self._universe.macro_series,
            behavioral_data=self._universe.behavioral_data,
            spy_df=self._universe.spy_df,
            settings=_settings,
        )

        trades = result.trades
        stats = compute_stats(trades)
        by_sig = group_by(trades, "signal_type")
        by_reg = group_by(trades, "market_regime")
        by_exit = group_by(trades, "exit_reason")
        by_year = group_by(trades, lambda t: str(t.entry_date.year)
        if t.entry_date else "<none>")

        elapsed = time.time() - t0
        logger.info(
            "  %-40s  %3d trades  E[R]=%+.3f  WR=%.0f%%  %.1fs",
            run_id, stats.trades_count, stats.expectancy_r,
            stats.win_rate * 100, elapsed,
        )

        return SweepPoint(
            run_id=run_id,
            param_name=param_name,
            param_value=param_value,
            param_label=param_label,
            group=group,
            is_baseline=is_baseline,
            stats=stats,
            by_signal=by_sig,
            by_regime=by_reg,
            by_exit=by_exit,
            by_year=by_year,
            n_tickers=len(self._universe.prepped),
            elapsed_s=elapsed,
            trades=trades,
        )


# ── worker helpers (module-level so ProcessPoolExecutor can pickle them) ──────

def _worker_init(src_root: str) -> None:
    """Called once per worker process to set up sys.path."""
    import sys
    src = os.path.join(src_root, "src")
    for p in (src_root, src):
        if p not in sys.path:
            sys.path.insert(0, p)


def _worker_run(
        packed: bytes,
        cfg: dict,
        port_params: dict,
        param_name: str,
        param_value: Any,
        param_label: str,
        group: str,
) -> SweepPoint:
    """
    Worker entry-point for ProcessPoolExecutor.

    Deserialises the universe snapshot, constructs the engine, and calls
    the same hot-path used in the sequential path.
    """
    universe = _unpack_universe(packed)

    engine_obj = _SweepRunHelper(universe, cfg, port_params)
    return engine_obj._run_one(
        cfg=cfg,
        port_params=port_params,
        param_name=param_name,
        param_value=param_value,
        param_label=param_label,
        group=group,
        is_baseline=False,
    )


class _SweepRunHelper:
    """Minimal stand-in for SweepEngine inside worker processes."""

    def __init__(self, universe, base_cfg, base_port):
        self._universe = universe
        self._base_cfg = base_cfg
        self._base_port = base_port

    _run_one = SweepEngine._run_one  # reuse the same method


# ── universe serialisation ────────────────────────────────────────────────────

def _pack_universe(uni: UniverseData) -> bytes:
    """Serialise a UniverseData to bytes for IPC."""
    import pickle
    return pickle.dumps(uni, protocol=pickle.HIGHEST_PROTOCOL)


def _unpack_universe(data: bytes) -> UniverseData:
    import pickle
    return pickle.loads(data)


# ── nested-dict helpers ───────────────────────────────────────────────────────

def _set_nested(d: dict, dotted: str, value: Any) -> None:
    """
    Set a value at a dotted path in a nested dict, creating missing keys.

    Example
    -------
    _set_nested(cfg, "signals.stop_loss.atr_multiplier", 2.5)
    # equivalent to: cfg["signals"]["stop_loss"]["atr_multiplier"] = 2.5
    """
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _get_nested(d: dict, dotted: str, default: Any = None) -> Any:
    """Get a value at a dotted path; return default if any key is missing."""
    parts = dotted.split(".")
    node = d
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# Mapping from filters.yaml dotted paths → settings.yaml dotted paths.
# These params live in settings.yaml (read by SignalScorer) but were
# historically specified as filters.yaml paths in the sweep grid.
_SETTINGS_ALIASES: dict[str, str] = {
    # Original PARAM_GRID aliases
    "scanner.weights.near_52w_high": "scanner.weights.near_52w_high",
    "scanner.weights.far_from_52w_low": "scanner.weights.far_from_52w_low",
    "scanner.weights.rp_percentile": "scanner.weights.rp_percentile",
    "scanner.weights.ma200_slope": "scanner.weights.ma200_slope",
    "scanner.exit_weights.vbp_resistance": "scanner.exit_weights.vbp_resistance",
    "scanner.min_score_to_alert": "scanner.min_score_to_alert",
    "behavioral.breadth_divergence_penalty": "behavioral.breadth_divergence_penalty",
    "behavioral.size_multiplier_floor": "behavioral.size_multiplier_floor",
    # SCORING_GRID — entry thresholds
    "scanner.entry_thresholds.rsi_healthy_center": "scanner.entry_thresholds.rsi_healthy_center",
    "scanner.entry_thresholds.rsi_healthy_half_w": "scanner.entry_thresholds.rsi_healthy_half_w",
    "scanner.entry_thresholds.ma50_slope_scale": "scanner.entry_thresholds.ma50_slope_scale",
    "scanner.entry_thresholds.breakout_band_pct": "scanner.entry_thresholds.breakout_band_pct",
    "scanner.entry_thresholds.volume_spike_ratio": "scanner.entry_thresholds.volume_spike_ratio",
    "scanner.entry_thresholds.near_52w_high_pct_band": "scanner.entry_thresholds.near_52w_high_pct_band",
    "scanner.entry_thresholds.far_from_52w_low_pct_floor": "scanner.entry_thresholds.far_from_52w_low_pct_floor",
    # SCORING_GRID — entry weights
    "scanner.weights.trend_up": "scanner.weights.trend_up",
    "scanner.weights.ma50_slope": "scanner.weights.ma50_slope",
    "scanner.weights.volume_spike": "scanner.weights.volume_spike",
    "scanner.weights.rsi_healthy": "scanner.weights.rsi_healthy",
    "scanner.weights.breakout_20d": "scanner.weights.breakout_20d",
    "scanner.weights.macd_bullish": "scanner.weights.macd_bullish",
    "scanner.weights.relative_strength": "scanner.weights.relative_strength",
    "scanner.weights.weekly_trend": "scanner.weights.weekly_trend",
    "scanner.weights.bb_zscore": "scanner.weights.bb_zscore",
    # SCORING_GRID — exit thresholds
    "scanner.exit_thresholds.rsi_overbought_floor": "scanner.exit_thresholds.rsi_overbought_floor",
    "scanner.exit_thresholds.rsi_overbought_range": "scanner.exit_thresholds.rsi_overbought_range",
    "scanner.exit_thresholds.multi_bar_decay_max": "scanner.exit_thresholds.multi_bar_decay_max",
    "scanner.exit_thresholds.vol_expansion_ratio": "scanner.exit_thresholds.vol_expansion_ratio",
    # SCORING_GRID — exit weights
    "scanner.exit_weights.regime_flip": "scanner.exit_weights.regime_flip",
    "scanner.exit_weights.multi_bar_decay": "scanner.exit_weights.multi_bar_decay",
    "scanner.exit_weights.rsi_overbought": "scanner.exit_weights.rsi_overbought",
    "scanner.exit_weights.macd_cross_down": "scanner.exit_weights.macd_cross_down",
    "scanner.exit_weights.vol_expansion": "scanner.exit_weights.vol_expansion",
    "scanner.exit_weights.rs_divergence": "scanner.exit_weights.rs_divergence",
}


def _apply_settings_mutation(settings: dict, param_name: str, value: Any) -> None:
    """
    Mutate the settings.yaml dict for sweep params that live there.

    The SignalScorer reads weights, min_score_to_alert, and behavioral params
    from settings.yaml — NOT from filters.yaml. This function bridges the gap
    so that OFAT sweep params correctly affect scoring behavior.
    """
    settings_path = _SETTINGS_ALIASES.get(param_name)
    if settings_path is not None:
        _set_nested(settings, settings_path, value)


def _empty_point(
        param_name: str,
        param_value: Any,
        param_label: str,
        group: str,
        is_baseline: bool = False,
) -> SweepPoint:
    """Return a zeroed SweepPoint for failed runs."""
    return SweepPoint(
        run_id=f"{group}/{param_name}={param_value}",
        param_name=param_name,
        param_value=param_value,
        param_label=param_label,
        group=group,
        is_baseline=is_baseline,
        stats=Stats(),
    )
