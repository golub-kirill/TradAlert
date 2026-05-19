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

The key insight: the pre-loaded UniverseData is reused across every
window.  Only the PortfolioConfig date-window changes, so no I/O
occurs after initial data load.

Public API
──────────
    WalkForwardEngine(universe, base_cfg, base_port_cfg,
                      is_years, oos_years, step_months)
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
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from backtest.loader import UniverseData
from backtest.sweep import SweepEngine, SweepPoint

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
        lines = [
            f"  Walk-Forward: {len(self.results)} windows  "
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
    base_port_cfg : Portfolio parameters dict (max_concurrent, etc.).
    is_years      : In-sample window length in years.  Default 3.
    oos_years     : Out-of-sample window length in years.  Default 1.
    step_months   : Months to slide each window forward.  Default 6.
    """

    def __init__(
            self,
            universe: UniverseData,
            base_cfg: dict,
            base_port_cfg: dict,
            is_years: float = 3.0,
            oos_years: float = 1.0,
            step_months: int = 6,
    ) -> None:
        self._universe = universe
        self._base_cfg = base_cfg
        self._base_port_cfg = base_port_cfg
        self._is_years = is_years
        self._oos_years = oos_years
        self._step_months = step_months

        # Reuse SweepEngine's _run_one logic
        self._engine = SweepEngine(
            universe=universe,
            base_cfg=base_cfg,
            base_port_cfg=base_port_cfg,
            n_workers=0,
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
        Run IS + OOS baseline for every rolling window.

        Returns WalkForwardReport with per-window results.
        """
        wins = self.windows()
        results: list[WFResult] = []

        for win in wins:
            if progress:
                progress(f"Window {win.index:02d}  IS={win.is_start}→{win.is_end}")

            is_pt = self._run_window(win.is_start, win.is_end, win)
            oos_pt = self._run_window(win.oos_start, win.oos_end, win)

            if progress:
                progress(
                    f"  IS  {is_pt.stats.trades_count:3d}t  "
                    f"E[R]={is_pt.stats.expectancy_r:+.3f}  "
                    f"OOS {oos_pt.stats.trades_count:3d}t  "
                    f"E[R]={oos_pt.stats.expectancy_r:+.3f}"
                )

            results.append(WFResult(window=win, is_point=is_pt, oos_point=oos_pt))

        return WalkForwardReport(
            results=results,
            is_years=self._is_years,
            oos_years=self._oos_years,
            step_months=self._step_months,
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
