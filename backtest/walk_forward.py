"""
backtest/walk_forward.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Rolling walk-forward validation for the TradAlert backtest system.

Strategy
────────
Split the full date range into overlapping IS/OOS windows:

    |←──── IS (3yr) ────→|←─ OOS (1yr) ─→|
                      |←──── IS (3yr) ────→|←─ OOS (1yr) ─→|
                                        ...

With 8 years of data and a 6-month step, this yields ~9 windows,
each with an independent OOS period.

Walk-forward modes
──────────────────
  baseline (re_tune=False):  Run the *current* config on IS and OOS.
      Validates temporal stability but does not defeat parameter-tuning
      data-snooping.

  re-tune (re_tune=True):  For each IS window, run an OFAT sweep
      via SweepEngine.run_ofat(), pick the best-by-E[R] config, then
      apply *that* config to the OOS window.  The OOS cell is then a
      true "if I had only seen up to IS_end, what would I have shipped"
      test.  Sweep results are cached in-memory for fast re-runs.

The key insight: the pre-loaded UniverseData is reused across every
window.  Only the PortfolioConfig date-window changes, so no I/O
occurs after initial data load.

Public API
──────────
    WalkForwardEngine(universe, base_cfg, base_port_cfg,
                      is_years, oos_years, step_months, re_tune, grid)
        .run(progress)  → WalkForwardReport

    WalkForwardReport
        .windows        list[WFWindow]
        .is_stats        pd.DataFrame  (one row per window, IS metrics)
        .oos_stats       pd.DataFrame  (one row per window, OOS metrics)
        .degradation     float          IS_avg_er − OOS_avg_er
        .oos_positive    float          fraction of OOS windows with E[R]>0
        .summary_lines() list[str]
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from backtest.loader import UniverseData
from backtest.sweep import SweepEngine, SweepPoint, PARAM_GRID, ParamSpec

logger = logging.getLogger(__name__)


# ── window descriptor ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WFWindow:
    index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date

    @property
    def label(self) -> str:
        return f"W{self.index:02d}  IS={self.is_start}→{self.is_end}  OOS={self.oos_start}→{self.oos_end}"

    @property
    def is_years(self) -> float:
        return (self.is_end - self.is_start).days / 365.25

    @property
    def oos_years(self) -> float:
        return (self.oos_end - self.oos_start).days / 365.25


# ── per-window result ─────────────────────────────────────────────────────────

@dataclass
class WFResult:
    window: WFWindow
    is_point: SweepPoint
    oos_point: SweepPoint
    tuned_params: dict = field(default_factory=dict)  # params chosen from IS sweep

    @property
    def is_er(self) -> float: return self.is_point.stats.expectancy_r

    @property
    def oos_er(self) -> float: return self.oos_point.stats.expectancy_r

    @property
    def degradation(self) -> float: return self.is_er - self.oos_er

    @property
    def oos_positive(self) -> bool: return self.oos_er > 0


# ── full report ───────────────────────────────────────────────────────────────

@dataclass
class WalkForwardReport:
    results: list[WFResult]
    is_years: float
    oos_years: float
    step_months: int
    re_tune: bool = False  # whether re-tuning was used

    @property
    def oos_er_values(self) -> list[float]:
        return [r.oos_er for r in self.results]

    @property
    def is_er_values(self) -> list[float]:
        return [r.is_er for r in self.results]

    @property
    def avg_is_er(self) -> float:
        v = self.is_er_values
        return sum(v) / len(v) if v else 0.0

    @property
    def avg_oos_er(self) -> float:
        v = self.oos_er_values
        return sum(v) / len(v) if v else 0.0

    @property
    def degradation(self) -> float:
        """Average IS E[R] minus average OOS E[R].  Lower is better."""
        return self.avg_is_er - self.avg_oos_er

    @property
    def pct_oos_positive(self) -> float:
        """Fraction of OOS windows with positive E[R]."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.oos_positive) / len(self.results)

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        for r in self.results:
            rows.append({
                "window": r.window.label,
                "is_start": r.window.is_start,
                "is_end": r.window.is_end,
                "oos_start": r.window.oos_start,
                "oos_end": r.window.oos_end,
                "is_trades": r.is_point.stats.trades_count,
                "is_wr": r.is_point.stats.win_rate,
                "is_er": r.is_point.stats.expectancy_r,
                "is_total_r": r.is_point.stats.total_r,
                "oos_trades": r.oos_point.stats.trades_count,
                "oos_wr": r.oos_point.stats.win_rate,
                "oos_er": r.oos_point.stats.expectancy_r,
                "oos_total_r": r.oos_point.stats.total_r,
                "degradation": r.degradation,
                "oos_positive": r.oos_positive,
            })
        return pd.DataFrame(rows)

    def summary_lines(self) -> list[str]:
        mode_tag = " [RE-TUNE]" if self.re_tune else ""
        lines = [
            f"  Walk-Forward{mode_tag}: {len(self.results)} windows  "
            f"({self.is_years:.0f}yr IS / {self.oos_years:.0f}yr OOS / "
            f"{self.step_months}mo step)",
            "",
            f"  {'Window':<8} {'IS trades':>9} {'IS E[R]':>8} "
            f"{'OOS trades':>10} {'OOS E[R]':>9} {'Δ':>7} {'OOS+':>5}",
            "  " + "─" * 60,
        ]
        for r in self.results:
            oos_flag = "✓" if r.oos_positive else "✗"
            lines.append(
                f"  W{r.window.index:02d}     "
                f"{r.is_point.stats.trades_count:>9}  "
                f"{r.is_er:>+8.3f}  "
                f"{r.oos_point.stats.trades_count:>10}  "
                f"{r.oos_er:>+9.3f}  "
                f"{r.degradation:>+7.3f}  "
                f"{oos_flag:>5}"
            )
        lines += [
            "  " + "─" * 60,
            f"  {'Average':<8} {'':>9}  "
            f"{self.avg_is_er:>+8.3f}  {'':>10}  "
            f"{self.avg_oos_er:>+9.3f}  "
            f"{self.degradation:>+7.3f}  "
            f"{self.pct_oos_positive:>5.0%}",
            "",
            f"  Degradation IS→OOS : {self.degradation:+.3f} E[R]",
            f"  OOS profitable     : {self.pct_oos_positive:.0%} of windows",
        ]
        if self.re_tune:
            lines.append("")
            lines.append("  Re-tuning: IS sweep → best config → OOS")
            for r in self.results:
                if r.tuned_params:
                    params_str = ", ".join(
                        f"{k}={v}" for k, v in list(r.tuned_params.items())[:4]
                    )
                    lines.append(
                        f"    W{r.window.index:02d} tuned: {params_str}"
                    )
        return lines


# ── engine ────────────────────────────────────────────────────────────────────

class WalkForwardEngine:
    """
    Rolling walk-forward validation engine.

    Reuses the pre-loaded UniverseData across all windows — no I/O
    after initialisation.  Each window simply applies a date filter
    to the timeline inside run_prepped().

    Parameters
    ----------
    universe      : Pre-loaded UniverseData (from loader.load_universe).
    base_cfg      : FilterEngine config dict (filters.yaml content).
    base_port_cfg : Portfolio parameters dict (max_open_risk, etc.).
    is_years      : In-sample window length in years.  Default 3.
    oos_years     : Out-of-sample window length in years.  Default 1.
    step_months   : Months to slide each window forward.  Default 6.
    re_tune       : If True, run OFAT sweep on IS and apply best to OOS.
    grid          : ParamSpec list for the OFAT sweep. Defaults to PARAM_GRID.
    """

    # Trade-count floor for IS parameter selection: a combo must clear this many
    # IS trades to be eligible as "best", so a tiny-sample fluke E[R] cannot win
    # selection. Falls back to the full set when no combo clears it (sparse window).
    _MIN_IS_TRADES: int = 20

    @staticmethod
    def _select_best_is(points, min_trades: int):
        """Pick the highest-E[R] sweep point that clears the trade-count floor.

        Falls back to the unfiltered set if the floor would leave nothing, so a
        window with few trades still yields a selection.
        """
        eligible = [p for p in points if p.stats.trades_count >= min_trades] or list(points)
        return max(eligible, key=lambda p: p.stats.expectancy_r)

    def __init__(
            self,
            universe: UniverseData,
            base_cfg: dict,
            base_port_cfg: dict,
            is_years: float = 3.0,
            oos_years: float = 1.0,
            step_months: int = 6,
            re_tune: bool = False,
            grid: list[ParamSpec] | None = None,
            n_workers: int = 0,
            use_scoring: bool = False,
    ) -> None:
        self._universe = universe
        self._base_cfg = base_cfg
        self._base_port_cfg = base_port_cfg
        self._is_years = is_years
        self._oos_years = oos_years
        self._step_months = step_months
        self._re_tune = re_tune
        self._use_scoring = use_scoring
        self._grid = grid if grid is not None else PARAM_GRID
        # Parallel workers for the per-window re-tune sweep (re_tune=True).
        # 0/1 = sequential. The baseline (re_tune=False) path runs single
        # _run_one calls and does not use the pool.
        self._n_workers = n_workers

        # In-memory sweep cache keyed by (is_start, is_end)
        self._sweep_cache: dict[tuple[date, date], SweepPoint] = {}

        # Reuse SweepEngine's _run_one logic
        self._engine = SweepEngine(
            universe=universe,
            base_cfg=base_cfg,
            base_port_cfg=base_port_cfg,
            n_workers=0,
            use_scoring=use_scoring,
        )

    # ── public ────────────────────────────────────────────────────────────────

    def windows(self) -> list[WFWindow]:
        """Generate all IS/OOS window descriptors."""
        first = self._universe.date_range.first
        last = self._universe.date_range.last

        is_delta = timedelta(days=int(self._is_years * 365.25))
        oos_delta = timedelta(days=int(self._oos_years * 365.25))

        windows: list[WFWindow] = []
        idx = 0
        cursor = first

        while True:
            is_start = cursor
            is_end = is_start + is_delta
            oos_start = is_end + timedelta(days=1)
            oos_end = oos_start + oos_delta

            if oos_end > last:
                break

            windows.append(WFWindow(
                index=idx,
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
            ))
            idx += 1

            # Advance cursor by step_months
            month = cursor.month - 1 + self._step_months
            new_year = cursor.year + month // 12
            new_month = month % 12 + 1
            cursor = cursor.replace(year=new_year, month=new_month)

        return windows

    def run(
            self,
            progress: Callable[[str], None] | None = None,
    ) -> WalkForwardReport:
        """
        Run IS + OOS for every rolling window.

        If re_tune=True:
          1. Run OFAT sweep on IS window (cached in-memory).
          2. Pick best config by E[R].
          3. Apply that config to OOS window.

        If re_tune=False (baseline):
          Run the base config on both IS and OOS.

        Returns WalkForwardReport with per-window results.
        """
        wins = self.windows()
        results: list[WFResult] = []

        for win in wins:
            if progress:
                progress(f"Window {win.index:02d}  IS={win.is_start}→{win.is_end}")

            if self._re_tune:
                is_pt, oos_pt, tuned = self._run_window_with_tuning(win, progress)
            else:
                is_pt = self._run_window(win.is_start, win.is_end, win)
                oos_pt = self._run_window(win.oos_start, win.oos_end, win)
                tuned = {}

            if progress:
                progress(
                    f"  IS  {is_pt.stats.trades_count:3d}t  "
                    f"E[R]={is_pt.stats.expectancy_r:+.3f}  "
                    f"OOS {oos_pt.stats.trades_count:3d}t  "
                    f"E[R]={oos_pt.stats.expectancy_r:+.3f}"
                )

            results.append(WFResult(
                window=win, is_point=is_pt, oos_point=oos_pt,
                tuned_params=tuned,
            ))

        return WalkForwardReport(
            results=results,
            is_years=self._is_years,
            oos_years=self._oos_years,
            step_months=self._step_months,
            re_tune=self._re_tune,
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _run_window(
            self,
            start: date,
            end: date,
            window: WFWindow,
    ) -> SweepPoint:
        """Run baseline with date window and return a SweepPoint."""
        port_params = dict(self._base_port_cfg)
        port_params["start_date"] = start
        port_params["end_date"] = end

        return self._engine._run_one(
            cfg=copy.deepcopy(self._base_cfg),
            port_params=port_params,
            param_name="walk_forward",
            param_value=f"{start}:{end}",
            param_label=f"W{window.index:02d}",
            group="walk_forward",
            is_baseline=False,
        )

    def _run_window_with_tuning(
            self,
            window: WFWindow,
            progress: Callable[[str], None] | None = None,
    ) -> tuple[SweepPoint, SweepPoint, dict]:
        """
        Run OFAT sweep on IS, pick best config, apply to OOS.

        Returns (is_point, oos_point, tuned_params_dict).
        The IS point is the best-tuned config's in-sample result; the OOS point is
        that same config out-of-sample → degradation = tuned-IS − tuned-OOS.
        """
        cache_key = (window.is_start, window.is_end)

        # Check in-memory cache
        if cache_key in self._sweep_cache:
            best_is = self._sweep_cache[cache_key]
        else:
            # Run OFAT sweep restricted to IS date window
            if progress:
                progress(f"    Sweing IS {window.is_start}→{window.is_end}...")

            sweep_engine = SweepEngine(
                universe=self._universe,
                base_cfg=copy.deepcopy(self._base_cfg),
                base_port_cfg=dict(self._base_port_cfg),
                n_workers=self._n_workers,
                use_scoring=self._use_scoring,
            )

            # Override port params to restrict to IS window
            original_port = sweep_engine._base_port
            sweep_engine._base_port = dict(original_port)
            sweep_engine._base_port["start_date"] = window.is_start
            sweep_engine._base_port["end_date"] = window.is_end

            sweep_report = sweep_engine.run_ofat(
                grid=self._grid,
                port_grid=[],
                progress=None,  # suppress individual sweep progress
            )

            # Pick best by E[R], subject to a trade-count floor (no fluke wins).
            all_pts = sweep_report.all_points
            best_is = self._select_best_is(all_pts, self._MIN_IS_TRADES)
            self._sweep_cache[cache_key] = best_is

        # Extract tuned params from the best sweep point
        tuned_params = {}
        if not best_is.is_baseline:
            tuned_params[best_is.param_name] = best_is.param_value

        # Run OOS with the best-tuned config
        oos_pt = self._run_window_with_config(
            window.oos_start, window.oos_end, window,
            best_is.param_name, best_is.param_value,
        )

        # IS point: the SAME tuned config's in-sample result (best_is is the IS
        # sweep point that was selected). Reporting tuned-IS vs tuned-OOS makes
        # degradation an honest overfitting measure — the old baseline-IS-vs-
        # tuned-OOS understated it (different configs on each side).
        is_pt = best_is

        return is_pt, oos_pt, tuned_params

    def _run_window_with_config(
            self,
            start: date,
            end: date,
            window: WFWindow,
            param_name: str,
            param_value: object,
    ) -> SweepPoint:
        """Run OOS with a specific parameter mutation."""
        from backtest.sweep import _set_nested

        cfg = copy.deepcopy(self._base_cfg)
        if not param_name.startswith("portfolio."):
            _set_nested(cfg, param_name, param_value)

        port_params = dict(self._base_port_cfg)
        port_params["start_date"] = start
        port_params["end_date"] = end

        if param_name.startswith("portfolio."):
            key = param_name[len("portfolio."):]
            port_params[key] = param_value

        return self._engine._run_one(
            cfg=cfg,
            port_params=port_params,
            param_name="wf_tuned",
            param_value=f"{param_name}={param_value}",
            param_label=f"W{window.index:02d}-tuned",
            group="walk_forward",
            is_baseline=False,
        )
