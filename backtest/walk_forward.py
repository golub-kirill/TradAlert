"""
Rolling walk-forward validation for the TradAlert backtest system.

Split the full date range into overlapping IS/OOS windows:

    |←──── IS (3yr) ────→|←─ OOS (1yr) ─→|
                      |←──── IS (3yr) ────→|←─ OOS (1yr) ─→|

~9 windows from 8 years of data at a 6-month step, each with an
independent OOS period.

Walk-forward modes
──────────────────
  baseline (re_tune=False):  Run the current config on IS and OOS.
      Tests temporal stability only; does not defeat parameter-tuning
      data-snooping.

  re-tune (re_tune=True):  Per IS window, run an OFAT sweep, pick the
      best-by-E[R] config, apply it to OOS — a true "if I had only seen
      up to IS_end, what would I have shipped" test. Sweeps cached in-memory.

  joint re-tune (re_tune=True, joint_samples>0):  Same protocol with
      SweepEngine.run_random_joint() — N seeded multi-knob configs instead
      of one-factor-at-a-time. OFAT ships one knob per window and so
      understates multi-parameter overfitting; joint mode reproduces that
      selection with an explicit per-window trial count (the input a
      deflated-Sharpe correction needs).

The pre-loaded UniverseData is reused across every window; only the
PortfolioConfig date-window changes, so no I/O occurs after initial load.

Public API
──────────
    WalkForwardEngine(universe, base_cfg, base_port_cfg, ...).run(progress)
        → WalkForwardReport
    WalkForwardReport: .results, .to_dataframe(), .degradation,
        .pct_oos_positive, .summary_lines()
"""

from __future__ import annotations

import copy
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from backtest.loader import UniverseData
from backtest.sweep import SweepEngine, SweepPoint, PARAM_GRID, ParamSpec

logger = logging.getLogger(__name__)


def _advance_months(d: date, months: int) -> date:
    """Add ``months`` to ``d``, clamping the day to the target month's length.

    ``date.replace(month=...)`` keeps the original day, so a 29/30/31 start date
    landing on a shorter month would raise ValueError. Clamping makes window
    stepping safe for any start date; days <= 28 are unchanged.
    """
    import calendar
    month_idx = d.month - 1 + months
    year = d.year + month_idx // 12
    month = month_idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


# ── window descriptor ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WFWindow:
    index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date
    # Trading bars skipped between is_end and oos_start (0 = adjacent sessions).
    # Recorded per window so a report can state the embargo actually applied
    # rather than the one requested — they differ at the end of the data.
    embargo_bars: int = 0

    @property
    def label(self) -> str:
        gap = f"  embargo={self.embargo_bars}b" if self.embargo_bars else ""
        return (f"W{self.index:02d}  IS={self.is_start}→{self.is_end}  "
                f"OOS={self.oos_start}→{self.oos_end}{gap}")

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
    re_tune: bool = False      # whether re-tuning was used
    joint_samples: int = 0     # >0 → randomized multi-knob re-tune (N per window)
    joint_seed: int = 0        # sampler seed (surfaced so seed-shopping stays visible)

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
        if self.re_tune and self.joint_samples:
            mode_tag = (f" [RE-TUNE/JOINT ×{self.joint_samples} "
                        f"seed={self.joint_seed}]")
        elif self.re_tune:
            mode_tag = " [RE-TUNE]"
        else:
            mode_tag = ""
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
                else:
                    # Surface silent baseline wins: the baseline competes in IS
                    # selection, and "no candidate beat it" is itself a result.
                    lines.append(
                        f"    W{r.window.index:02d} tuned: baseline retained "
                        f"(no candidate beat it in-sample)"
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
    # IS trades to be eligible as "best", so a tiny-sample fluke E[R] cannot win.
    _MIN_IS_TRADES: int = 20

    @staticmethod
    def _select_best_is(points, min_trades: int):
        """Pick the highest-E[R] sweep point that clears the trade-count floor.

        Fallback ladder: combos clearing the floor → any combo that actually
        traded → the baseline. Never selects a 0-trade point, so a window whose
        workers all crashed (every point zeroed) degrades to baseline-config OOS
        rather than tuning the OOS leg on a junk config.
        """
        if not points:
            return None
        eligible = [p for p in points if p.stats.trades_count >= min_trades]
        if not eligible:
            eligible = [p for p in points if p.stats.trades_count > 0]
        if not eligible:
            baseline = next((p for p in points if getattr(p, "is_baseline", False)), None)
            return baseline if baseline is not None else points[0]
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
            joint_samples: int = 0,
            joint_knobs: int = 3,
            joint_seed: int = 1337,
            embargo_bars: int = 0,
    ) -> None:
        self._universe = universe
        self._base_cfg = base_cfg
        self._base_port_cfg = base_port_cfg
        self._is_years = is_years
        self._oos_years = oos_years
        self._step_months = step_months
        self._re_tune = re_tune
        self._grid = grid if grid is not None else PARAM_GRID
        # Joint re-tune: >0 replaces the per-window OFAT sweep with N seeded
        # multi-knob samples (joint_knobs mutated per sample). Seed is offset
        # by window index so each window draws its own reproducible candidates.
        self._joint_samples = max(0, int(joint_samples))
        self._joint_knobs = max(1, int(joint_knobs))
        self._joint_seed = int(joint_seed)
        # Parallel workers for the per-window re-tune sweep. 0/1 = sequential.
        # The baseline (re_tune=False) path runs single _run_one calls, no pool.
        self._n_workers = n_workers
        # Embargo in TRADING bars between is_end and oos_start. 0 (default) =
        # adjacent sessions, the historical behaviour. The pre-registered setting
        # is max_hold_days (25) — the hard bound on the label horizon, hence the
        # shortest gap that fully decorrelates the seam under max_hold_mode
        # "hard". Under "if_not_profit" a winner may still outrun it; those
        # trades are removed by the purge instead, and counted.
        self._embargo_bars = max(0, int(embargo_bars))
        self._calendar: list[date] | None = None

        # In-memory sweep cache keyed by (is_start, is_end)
        self._sweep_cache: dict[tuple[date, date], SweepPoint] = {}

        # Reuse SweepEngine's _run_one logic
        self._engine = SweepEngine(
            universe=universe,
            base_cfg=base_cfg,
            base_port_cfg=base_port_cfg,
            n_workers=0,
        )

    # ── public ────────────────────────────────────────────────────────────────

    def _trading_days(self) -> list[date]:
        """Union session calendar across the universe, ascending. Cached.

        The embargo is specified in TRADING bars (it mirrors a label horizon
        measured in bars), so it has to be applied on sessions — a calendar-day
        offset would silently vary with weekends and holidays.
        """
        if self._calendar is None:
            days = {ts.date() for prep in self._universe.prepped.values()
                    for ts in prep.df.index}
            self._calendar = sorted(days)
        return self._calendar

    def windows(self) -> list[WFWindow]:
        """Generate all IS/OOS window descriptors."""
        first = self._universe.date_range.first
        last = self._universe.date_range.last

        is_delta = timedelta(days=int(self._is_years * 365.25))
        oos_delta = timedelta(days=int(self._oos_years * 365.25))
        cal = self._trading_days()
        embargo = max(0, int(self._embargo_bars))

        windows: list[WFWindow] = []
        idx = 0
        cursor = first

        while True:
            is_start = cursor
            is_end = is_start + is_delta

            # OOS opens on the first session after is_end, pushed out by the
            # embargo. The gap absorbs trades still resolving at the seam, so a
            # config is not scored on a market state contiguous with the one it
            # was tuned on.
            seam = bisect_right(cal, is_end)
            oos_i = seam + embargo
            if oos_i >= len(cal):
                break
            oos_start = cal[oos_i]
            oos_end = oos_start + oos_delta

            if oos_end > last:
                break

            windows.append(WFWindow(
                index=idx,
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                embargo_bars=oos_i - seam,
            ))
            idx += 1

            # Advance cursor by step_months (day clamped to the target month).
            cursor = _advance_months(cursor, self._step_months)

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
                is_pt = self._run_window(win.is_start, win.is_end, win, purge=True)
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
            joint_samples=self._joint_samples if self._re_tune else 0,
            joint_seed=self._joint_seed if self._re_tune else 0,
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _run_window(
            self,
            start: date,
            end: date,
            window: WFWindow,
            *,
            purge: bool = False,
    ) -> SweepPoint:
        """Run baseline with date window and return a SweepPoint.

        ``purge`` is set for the IS leg only: an in-sample trade whose exit lands
        on/after ``window.oos_start`` overlaps the test block and is dropped. The
        OOS leg is never purged — there is no later training inside the window for
        it to leak into, and trimming it would discard genuine results.
        """
        port_params = dict(self._base_port_cfg)
        port_params["start_date"] = start
        port_params["end_date"] = end
        # Set explicitly on both branches: leaving the OOS leg to inherit
        # whatever base_port_cfg happens to hold would make "OOS is unpurged" an
        # accident of the caller rather than an invariant of this method.
        port_params["purge_exit_from"] = window.oos_start if purge else None

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
        Run the IS sweep (OFAT, or randomized joint when joint_samples>0),
        pick the best config, apply it to OOS.

        Returns (is_point, oos_point, tuned_params_dict).
        The IS point is the best-tuned config's in-sample result; the OOS point is
        that same config out-of-sample → degradation = tuned-IS − tuned-OOS.
        """
        cache_key = (window.is_start, window.is_end)

        # Check in-memory cache
        if cache_key in self._sweep_cache:
            best_is = self._sweep_cache[cache_key]
        else:
            # Run the IS-restricted sweep
            if progress:
                progress(f"    Sweeping IS {window.is_start}→{window.is_end}...")

            sweep_engine = SweepEngine(
                universe=self._universe,
                base_cfg=copy.deepcopy(self._base_cfg),
                base_port_cfg=dict(self._base_port_cfg),
                n_workers=self._n_workers,
            )

            # Override port params to restrict to IS window
            original_port = sweep_engine._base_port
            sweep_engine._base_port = dict(original_port)
            sweep_engine._base_port["start_date"] = window.is_start
            sweep_engine._base_port["end_date"] = window.is_end
            # Purge EVERY candidate, not just the winner: the sweep picks the
            # best config by in-sample E[R], so leaving OOS-overlapping trades in
            # would let the selection itself be made on contaminated statistics —
            # the leak this whole change exists to close.
            sweep_engine._base_port["purge_exit_from"] = window.oos_start

            if self._joint_samples > 0:
                sweep_report = sweep_engine.run_random_joint(
                    n_samples=self._joint_samples,
                    knobs=self._joint_knobs,
                    seed=self._joint_seed + window.index,
                    grid=self._grid,
                    port_grid=[],
                    progress=None,  # suppress individual sweep progress
                )
            else:
                sweep_report = sweep_engine.run_ofat(
                    grid=self._grid,
                    port_grid=[],
                    progress=None,
                )

            # Pick best by E[R], subject to a trade-count floor (no fluke wins).
            all_pts = sweep_report.all_points
            best_is = self._select_best_is(all_pts, self._MIN_IS_TRADES)
            self._sweep_cache[cache_key] = best_is

        # Tuned mutation set from the best sweep point: joint points carry the
        # full multi-knob dict; OFAT points carry their single knob; the
        # baseline carries nothing (OOS replays base config).
        tuned_params: dict = {}
        if not best_is.is_baseline:
            if getattr(best_is, "mutations", None):
                tuned_params = dict(best_is.mutations)
            else:
                tuned_params[best_is.param_name] = best_is.param_value

        # Run OOS with the best-tuned config
        oos_pt = self._run_window_with_mutations(
            window.oos_start, window.oos_end, window, tuned_params,
        )

        # IS point = the SAME tuned config's in-sample result (best_is is the
        # selected IS sweep point). Tuned-IS vs tuned-OOS keeps degradation an
        # honest overfitting measure (same config on both sides).
        is_pt = best_is

        return is_pt, oos_pt, tuned_params

    def _run_window_with_mutations(
            self,
            start: date,
            end: date,
            window: WFWindow,
            mutations: dict,
    ) -> SweepPoint:
        """Run a date-window with a ``{dotted: value}`` mutation set applied.

        An empty dict replays the unmodified base config (the IS winner was
        the baseline).
        """
        from backtest.sweep import _set_nested

        cfg = copy.deepcopy(self._base_cfg)
        port_params = dict(self._base_port_cfg)

        for param_name, param_value in mutations.items():
            if param_name.startswith("portfolio."):
                port_params[param_name[len("portfolio."):]] = param_value
            else:
                _set_nested(cfg, param_name, param_value)

        port_params["start_date"] = start
        port_params["end_date"] = end
        # The OOS leg is never purged: there is no later training inside the
        # window for it to leak into, and dropping trades that resolve past
        # oos_end would discard genuine results. Popped rather than merely left
        # unset, so a caller that puts purge_exit_from in base_port_cfg cannot
        # silently turn it on here.
        port_params.pop("purge_exit_from", None)

        desc = ", ".join(f"{k}={v}" for k, v in mutations.items()) or "baseline"

        return self._engine._run_one(
            cfg=cfg,
            port_params=port_params,
            param_name="wf_tuned",
            param_value=desc,
            param_label=f"W{window.index:02d}-tuned",
            group="walk_forward",
            is_baseline=False,
            # Route every knob through the settings channel too; otherwise
            # settings-resident winners (e.g. behavioral.size_mult_floor) replay
            # baseline settings on the OOS leg.
            mutations=mutations,
        )
