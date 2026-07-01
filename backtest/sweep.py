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
    SweepEngine.baseline()              -> SweepPoint
    SweepEngine.run_ofat(grid)          -> SweepReport
    SweepEngine.run_random_joint(n, k)  -> SweepReport  (multi-knob samples)

    PARAM_GRID                      : default OFAT spec (17 parameters)
    PORTFOLIO_GRID                  : portfolio-level sweep spec
"""

from __future__ import annotations

import copy
import logging
import multiprocessing
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd
import yaml

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

    # Momentum exit (fade trigger). NB `signals.momentum.short` is the held-LONG
    # momentum-fade EXIT (legacy name), not a short entry. The rsi_min floor below
    # rarely binds (at a MACD zero-cross-down RSI is usually well above 40), so
    # sweeping it often shows little effect — an inert gate, not a wiring bug.
    # See docs/triage_raw_notes_2026-06.md (Note 2a).
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

    # Behavioral size-multiplier floor. Consumer key is `size_mult_floor`
    # (settings.yaml + behavioral classifier); the alias must match that spelling
    # or this row is a no-op.
    ParamSpec("behavioral.size_mult_floor",
              (0.25, 0.35, 0.50, 0.65),
              "Behavioral size floor", "phase8"),
]

PORTFOLIO_GRID: list[ParamSpec] = [
    ParamSpec("portfolio.max_open_risk",
              (2.0, 3.0, 5.0, 8.0, 10.0),
              "Max open risk (size_mult units)", "portfolio",
              fmt="{:.1f}"),
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

    # Dynamic exits. These dimensions were searched, so they must sit in the grid
    # for honest multiple-testing trial counts — the deflated-Sharpe N is only as
    # truthful as the grid it sweeps.
    ParamSpec("portfolio.breakeven_trigger_r",
              (0.5, 0.75, 1.0, 1.25, 1.5),
              "Breakeven stop trigger (R)", "exits",
              fmt="{:.2f}"),
    ParamSpec("portfolio.trail_atr_mult",
              (3.0, 5.0, 6.0, 8.0),
              "ATR trail multiplier", "exits",
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

    # Full {dotted: value} mutation set behind this point. OFAT points carry
    # their single knob; joint points carry every mutated knob — the
    # walk-forward OOS leg replays exactly this dict.
    mutations: dict | None = None


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
                "direction": t.direction,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "initial_stop": t.initial_stop,
                "initial_target": t.initial_target,
                "exit_reason": t.exit_reason,
                "r_multiple": round(t.r_multiple, 4),
                # size- and borrow-adjusted R (= r_multiple × size_mult − borrow drag).
                # validate_shorts uses this for the economic Sharpe/Calmar checks so the
                # short side isn't judged on raw per-unit R.
                "effective_r": round(t.effective_r, 4),
                "size_mult": round(t.size_mult, 4),
                "borrow_annual_rate": round(t.borrow_annual_rate, 5),
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
            "max_open_risk": 5.0,
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
            progress("Running baseline... (one full backtest; no config progress until it finishes)")
        base_pt = self.baseline()
        logger.info("Baseline: %d trades E[R]=%+.3f",
                    base_pt.stats.trades_count, base_pt.stats.expectancy_r)
        if progress:
            _bs = base_pt.stats
            progress(f"baseline done: {_bs.trades_count}t E[R]{_bs.expectancy_r:+.3f} "
                     f"WR{_bs.win_rate:.0%} PF{_bs.profit_factor:.2f}")

        # ── build job list ────────────────────────────────────────────────
        jobs: list[dict] = []
        for spec in all_specs:
            baseline_val = self._resolve_baseline(spec)
            for val in spec.values:
                if val == baseline_val:
                    continue  # skip exact baseline -- already have it
                jobs.append({
                    "param_name": spec.dotted,
                    "param_value": val,
                    "param_label": spec.label,
                    "group": spec.group,
                    "mutations": {spec.dotted: val},
                    "progress_text": f"{spec.label} = {spec.fmt.format(val)}",
                })

        logger.info("Sweep: %d jobs, %d workers", len(jobs), self._n_workers)

        # ── execute ───────────────────────────────────────────────────────
        points: list[SweepPoint] = []

        if self._n_workers <= 1:
            for job in jobs:
                if progress:
                    progress(job["progress_text"])
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

    def run_random_joint(
            self,
            n_samples: int,
            knobs: int = 3,
            seed: int = 1337,
            grid: list[ParamSpec] | None = None,
            port_grid: list[ParamSpec] | None = None,
            progress: Callable[[str], None] | None = None,
    ) -> SweepReport:
        """
        Randomized multi-knob sweep: each sample mutates ``knobs`` parameters
        jointly, drawn from the same grids OFAT uses.

        OFAT moves one knob per run, so selecting its winner cannot reproduce
        a multi-parameter selection and understates overfitting. Joint samples
        give walk-forward re-tuning an honest multi-knob search space with an
        explicit trial count (``n_samples``) for multiple-testing corrections.
        The sampler is seeded → the same (seed, grids, live baseline values)
        always yields the same candidate set; per-spec pools exclude the live
        baseline value, so editing a config default shifts the draws.

        Returns a SweepReport whose points carry ``mutations`` (the full
        {dotted: value} dict) so the selected combo can be replayed exactly.
        """
        rng = random.Random(seed)
        grid = grid if grid is not None else PARAM_GRID
        port_grid = port_grid if port_grid is not None else PORTFOLIO_GRID
        all_specs = list(grid) + list(port_grid)

        t_total = time.time()

        if progress:
            progress("Running baseline... (one full backtest; no config progress until it finishes)")
        base_pt = self.baseline()
        logger.info("Baseline: %d trades E[R]=%+.3f",
                    base_pt.stats.trades_count, base_pt.stats.expectancy_r)
        if progress:
            _bs = base_pt.stats
            progress(f"baseline done: {_bs.trades_count}t E[R]{_bs.expectancy_r:+.3f} "
                     f"WR{_bs.win_rate:.0%} PF{_bs.profit_factor:.2f}")

        # Candidate pool: per spec, its non-baseline values.
        values_by_spec: dict[str, tuple[ParamSpec, list]] = {}
        for spec in all_specs:
            baseline_val = self._resolve_baseline(spec)
            vals = [v for v in spec.values if v != baseline_val]
            if vals:
                values_by_spec[spec.dotted] = (spec, vals)

        spec_keys = sorted(values_by_spec)  # sorted → seed-stable order
        k = min(knobs, len(spec_keys))
        if k == 0 or n_samples <= 0:
            return SweepReport(
                baseline=base_pt, points=[],
                universe_info=self._universe.summary(),
                elapsed_s=time.time() - t_total, n_workers=self._n_workers,
            )

        # Sample unique mutation sets (cap attempts in case the space is tiny).
        jobs: list[dict] = []
        seen: set[frozenset] = set()
        attempts = 0
        while len(jobs) < n_samples and attempts < n_samples * 50:
            attempts += 1
            chosen = rng.sample(spec_keys, k)
            mutations: dict = {}
            label_parts: list[str] = []
            for dotted in sorted(chosen):
                spec, vals = values_by_spec[dotted]
                val = rng.choice(vals)
                mutations[dotted] = val
                label_parts.append(f"{dotted}={spec.fmt.format(val)}")
            key = frozenset(mutations.items())
            if key in seen:
                continue
            seen.add(key)
            desc = ", ".join(label_parts)
            jobs.append({
                "param_name": "joint",
                "param_value": desc,
                "param_label": f"J{len(jobs):03d}",
                "group": "joint",
                "mutations": mutations,
                "progress_text": f"J{len(jobs):03d}: {desc}",
            })
        if len(jobs) < n_samples:
            logger.warning(
                "Joint sweep: only %d unique combos available (asked for %d)",
                len(jobs), n_samples)

        logger.info("Joint sweep: %d samples × %d knobs (seed=%d), %d workers",
                    len(jobs), k, seed, self._n_workers)

        points: list[SweepPoint] = []
        if self._n_workers <= 1:
            for job in jobs:
                if progress:
                    progress(job["progress_text"])
                points.append(self._dispatch_job(job))
        else:
            points = self._run_parallel(jobs, progress)

        elapsed = time.time() - t_total
        logger.info("Joint sweep complete in %.1fs -- %d points",
                    elapsed, len(points))

        return SweepReport(
            baseline=base_pt,
            points=points,
            universe_info=self._universe.summary(),
            elapsed_s=elapsed,
            n_workers=self._n_workers,
        )

    # ── private ───────────────────────────────────────────────────────────

    def _resolve_baseline(self, spec: ParamSpec) -> Any:
        """Extract the baseline value for a ParamSpec from the live configs.

        Settings-routed specs (behavioral.* — see ``_SETTINGS_ALIASES``)
        don't exist in filters.yaml, so they fall back to settings.yaml;
        without that, their live baseline values would enter the job pool
        as fake mutations and pad the trial count.
        """
        if spec.dotted.startswith("portfolio."):
            key = spec.dotted[len("portfolio."):]
            return self._base_port.get(key)
        parts = spec.dotted.split(".")
        node = self._base_cfg
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                node = None
                break
            node = node[p]
        if node is not None:
            return node
        settings_path = _SETTINGS_ALIASES.get(spec.dotted)
        if settings_path is not None:
            settings = self._load_settings()
            if settings is not None:
                return _get_nested(settings, settings_path)
        return None

    def _load_settings(self) -> dict | None:
        """Load and cache config/settings.yaml (baseline resolution only)."""
        if not hasattr(self, "_settings_cache"):
            try:
                import yaml as _yaml
                path = os.path.join(os.path.dirname(__file__), "..",
                                    "config", "settings.yaml")
                with open(path, encoding="utf-8") as f:
                    self._settings_cache = _yaml.safe_load(f)
            except Exception as exc:
                logger.debug("settings.yaml load failed for baseline "
                             "resolution: %s", exc)
                self._settings_cache = None
        return self._settings_cache

    def _materialise(self, mutations: dict) -> tuple[dict, dict]:
        """Build (cfg, port_params) with every ``{dotted: value}`` mutation applied."""
        cfg = copy.deepcopy(self._base_cfg)
        port_params = dict(self._base_port)
        for dotted, val in mutations.items():
            if dotted.startswith("portfolio."):
                port_params[dotted[len("portfolio."):]] = val
            else:
                _set_nested(cfg, dotted, val)
        return cfg, port_params

    def _dispatch_job(self, job: dict) -> SweepPoint:
        """Build args and call _run_one for a single job dict."""
        cfg, port_params = self._materialise(job["mutations"])

        pt = self._run_one(
            cfg=cfg,
            port_params=port_params,
            param_name=job["param_name"],
            param_value=job["param_value"],
            param_label=job["param_label"],
            group=job["group"],
            is_baseline=False,
            mutations=job["mutations"],
        )
        pt.mutations = dict(job["mutations"])
        return pt

    def _run_parallel(
            self,
            jobs: list[dict],
            progress: Callable | None,
    ) -> list[SweepPoint]:
        """Execute jobs across a ProcessPoolExecutor."""
        packed_universe = _pack_universe(self._universe)

        futures = {}
        points: list[SweepPoint] = [None] * len(jobs)  # preserve order
        src_path = os.path.join(os.path.dirname(__file__), "..")
        t0 = time.time()

        with ProcessPoolExecutor(
                max_workers=self._n_workers,
                initializer=_worker_init,
                initargs=(src_path, packed_universe),  # universe shipped ONCE per worker
        ) as pool:
            for i, job in enumerate(jobs):
                cfg, port_params = self._materialise(job["mutations"])

                fut = pool.submit(
                    _worker_run,
                    cfg,
                    port_params,
                    job["param_name"],
                    job["param_value"],
                    job["param_label"],
                    job["group"],
                    job["mutations"],
                )
                futures[fut] = i

            n_done = 0
            n_failed = 0
            for fut in as_completed(futures):
                idx = futures[fut]
                job = jobs[idx]
                try:
                    pt = fut.result()
                    points[idx] = pt
                except Exception as exc:
                    n_failed += 1
                    # Log the FULL worker traceback (exc_info), not just str(exc),
                    # so a BrokenProcessPool / OOM / pickling crash is diagnosable
                    # rather than silently swallowed into a zeroed point that
                    # downstream tuning treats as a real 0-trade config.
                    logger.error("Job failed [%s=%s] — substituting an empty point",
                                 job["param_label"], job["param_value"], exc_info=exc)
                    points[idx] = _empty_point(
                        job["param_name"], job["param_value"],
                        job["param_label"], job["group"],
                    )
                if points[idx] is not None:
                    points[idx].mutations = dict(job["mutations"])
                n_done += 1
                if progress:
                    msg = f"[{n_done}/{len(jobs)}] {job['progress_text']}"
                    try:                                   # enrich with the result (guarded)
                        s = points[idx].stats
                        msg += (f" → {s.trades_count}t E[R]{s.expectancy_r:+.3f} "
                                f"WR{s.win_rate:.0%}")
                    except Exception:
                        pass
                    _el = time.time() - t0
                    _eta = _el / n_done * (len(jobs) - n_done)
                    msg += f"  [{_el:.0f}s elapsed · ETA {_eta:.0f}s]"
                    progress(msg)

            if n_failed:
                logger.warning(
                    "Sweep: %d/%d jobs FAILED (tracebacks above) — their points are "
                    "zeroed; selection logic must exclude them, not treat them as "
                    "real 0-trade configs.", n_failed, len(jobs),
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
            mutations: dict | None = None,
    ) -> SweepPoint:
        """Hot-path: construct engine + portfolio config, run the backtest,
        collect stats. Built from module-level helpers shared with the worker
        stand-in; settings.yaml is cached per process so jobs don't re-read disk."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

        from core.filter_engine import FilterEngine
        from backtest.portfolio_backtester import PortfolioBacktester

        run_id = f"{group}/{param_name}={param_value}"
        t0 = time.time()

        try:
            engine = FilterEngine.from_dict(cfg)
        except Exception as exc:
            logger.warning("FilterEngine.from_dict failed [%s]: %s", run_id, exc)
            return _empty_point(param_name, param_value, param_label, group, is_baseline)

        pcfg = _build_port_config(port_params, run_id)
        if pcfg is None:
            return _empty_point(param_name, param_value, param_label, group, is_baseline)

        settings = _job_settings(param_name, param_value, is_baseline, mutations)

        bt = PortfolioBacktester(engine, pcfg)
        result = bt.run_prepped(
            self._universe.prepped,
            self._universe.skipped,
            self._universe.market_dfs,
            self._universe.vix_df,
            macro_series=self._universe.macro_series,
            behavioral_data=self._universe.behavioral_data,
            spy_df=self._universe.spy_df,
            settings=settings,
        )
        return _collect_point(
            result.trades, run_id, param_name, param_value, param_label,
            group, is_baseline, len(self._universe.prepped), t0,
        )


# ── sweep-job helpers (module-level so the worker stand-in reuses them) ───────

_PORT_FIELDS = frozenset({
    "max_open_risk", "start_date", "end_date", "earnings_aware",
    "close_open_at_eod", "entry_slippage_pct", "commission_r",
    "max_hold_days", "max_hold_mode",
    "trail_atr_mult", "trail_activate_r",
    "breakeven_trigger_r", "breakeven_buffer_atr",
    "correlation_cap", "correlation_lookback_days",
    "correlation_min_overlap", "correlation_floor",
})

# config/settings.yaml is read ONCE per process: each job deep-copies this base and
# applies its own mutations. _UNLOADED distinguishes "not yet read" from a cached
# read that yielded None (missing/unreadable file → fail-open).
_SETTINGS_UNLOADED = object()
_BASE_SETTINGS = _SETTINGS_UNLOADED


def _base_settings():
    """Parsed config/settings.yaml, cached per process. None when unreadable."""
    global _BASE_SETTINGS
    if _BASE_SETTINGS is _SETTINGS_UNLOADED:
        path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                _BASE_SETTINGS = yaml.safe_load(f)
        except Exception as exc:
            logger.debug("settings load failed — running without settings "
                         "context: %s", exc)
            _BASE_SETTINGS = None
    return _BASE_SETTINGS


def _job_settings(param_name: str, param_value: Any, is_baseline: bool,
                  mutations: dict | None):
    """Per-job settings: a deep copy of the cached base with this job's
    settings-resident mutations applied (behavioral params). None when the base
    is unreadable; baseline jobs get the base unmutated.

    When a ``mutations`` dict is given EVERY entry is routed — ``param_name``
    alone can't carry multi-knob jobs ("joint") or the walk-forward OOS replay
    ("wf_tuned"), whose settings-resident knobs would otherwise be silent no-ops.
    """
    base = _base_settings()
    if base is None:
        return None
    settings = copy.deepcopy(base)
    if not is_baseline:
        try:
            if mutations:
                for m_name, m_val in mutations.items():
                    _apply_settings_mutation(settings, m_name, m_val)
            else:
                _apply_settings_mutation(settings, param_name, param_value)
        except Exception as exc:
            logger.debug("settings mutation failed [%s=%s]: %s",
                         param_name, param_value, exc)
    return settings


def _build_port_config(port_params: dict, run_id: str):
    """Build the PortfolioConfig for one sweep job: filter port_params to the
    allowlisted fields and attach a fresh per-job chronic-loser tracker (so
    sweep points don't share streak state). Returns None on construction failure."""
    from backtest.portfolio_backtester import PortfolioConfig
    pcfg_kwargs = {k: v for k, v in port_params.items() if k in _PORT_FIELDS}
    chronic_cfg = port_params.get("chronic_loser_cfg")
    if chronic_cfg:
        try:
            from core.ticker_health import TickerHealth
            pcfg_kwargs["ticker_health"] = TickerHealth.from_config(chronic_cfg)
        except Exception as exc:
            logger.warning("TickerHealth.from_config failed [%s]: %s", run_id, exc)
    try:
        return PortfolioConfig(**pcfg_kwargs)
    except Exception as exc:
        logger.warning("PortfolioConfig failed [%s]: %s", run_id, exc)
        return None


def _collect_point(trades, run_id, param_name, param_value, param_label,
                   group, is_baseline, n_tickers, t0) -> SweepPoint:
    """Compute stats + group breakdowns and assemble the SweepPoint for one job."""
    stats = compute_stats(trades)
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
        by_signal=group_by(trades, "signal_type"),
        by_regime=group_by(trades, "market_regime"),
        by_exit=group_by(trades, "exit_reason"),
        by_year=group_by(trades, lambda t: str(t.entry_date.year)
                         if t.entry_date else "<none>"),
        n_tickers=n_tickers,
        elapsed_s=elapsed,
        trades=trades,
    )


# ── worker helpers (module-level so ProcessPoolExecutor can pickle them) ──────

# Per-worker universe cache. UniverseData is shipped to each worker process ONCE
# via the pool initializer (initargs) and unpickled here, rather than re-passed and
# re-deserialised on every job — which would make IPC the bottleneck and defeat
# --workers scaling. See _run_parallel.
_WORKER_UNIVERSE = None


def _worker_init(src_root: str, packed: bytes | None = None) -> None:
    """Called once per worker process: set up sys.path, then cache the universe.

    sys.path must be set before unpickling so UniverseData and its dependencies
    are importable.
    """
    import sys
    src = os.path.join(src_root, "src")
    for p in (src_root, src):
        if p not in sys.path:
            sys.path.insert(0, p)
    if packed is not None:
        global _WORKER_UNIVERSE
        _WORKER_UNIVERSE = _unpack_universe(packed)


def _worker_run(
        cfg: dict,
        port_params: dict,
        param_name: str,
        param_value: Any,
        param_label: str,
        group: str,
        mutations: dict | None = None,
) -> SweepPoint:
    """
    Worker entry-point for ProcessPoolExecutor.

    Uses the per-worker cached universe (set once in _worker_init) and calls
    the same hot-path used in the sequential path — no per-job deserialisation.
    """
    universe = _WORKER_UNIVERSE
    if universe is None:
        raise RuntimeError("worker universe not initialised (initargs missing)")

    engine_obj = _SweepRunHelper(universe, cfg, port_params)
    return engine_obj._run_one(
        cfg=cfg,
        port_params=port_params,
        param_name=param_name,
        param_value=param_value,
        param_label=param_label,
        group=group,
        is_baseline=False,
        mutations=mutations,
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
# These params live in settings.yaml but are specified as filters.yaml-style
# dotted paths in the sweep grid.
_SETTINGS_ALIASES: dict[str, str] = {
    "behavioral.size_mult_floor": "behavioral.size_mult_floor",
}


def _apply_settings_mutation(settings: dict, param_name: str, value: Any) -> None:
    """
    Mutate the settings.yaml dict for sweep params that live there.

    The behavioral classifier reads its params from settings.yaml — NOT from
    filters.yaml. This function bridges the gap so that settings-resident
    sweep params correctly reach the backtest.
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
