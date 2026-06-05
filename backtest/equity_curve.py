"""
backtest/equity_curve.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Convert a list of Trade objects (or raw R-multiple arrays) into
time-series analytics for reporting and charting.

All output is in R units.  Dollar conversion requires a bankroll
and position-sizing fraction, supplied optionally.

Public API
──────────
    build_curve(trades)  → EquityCurve
    EquityCurve
        .equity          pd.Series  (running cumulative R, indexed by date)
        .drawdown        pd.Series  (underwater depth from peak, in R)
        .monthly         pd.Series  (R per calendar month, "YYYY-MM" index)
        .annual          pd.Series  (R per calendar year, int index)
        .sharpe          float
        .sortino         float
        .calmar          float      (annual R / max_drawdown)
        .max_dd          float
        .recovery_days   int | None (bars to recover from worst drawdown)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from backtest.stats_utils import sharpe_ratio, sortino_ratio

if TYPE_CHECKING:
    from backtest.trade import Trade


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class EquityCurve:
    """
    Full time-series analytics for a backtest run.

    All values are in R units.
    """
    equity: pd.Series  # cumulative R, indexed by exit_date
    drawdown: pd.Series  # underwater depth (always ≥ 0)
    monthly: pd.Series  # R summed per "YYYY-MM"
    annual: pd.Series  # R summed per year (int index)
    sharpe: float  # annualised Sharpe from monthly R
    sortino: float  # annualised Sortino from monthly R
    calmar: float  # ann_r / max_dd  (NaN if max_dd = 0)
    max_dd: float  # peak-to-trough in R
    recovery_days: int | None  # calendar days to recover worst DD

    # derived ──────────────────────────────────────────────────────────────────

    @property
    def total_r(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else 0.0

    @property
    def annual_r(self) -> float:
        """Average R per calendar year."""
        if self.annual.empty:
            return 0.0
        return float(self.annual.mean())

    @property
    def best_month(self) -> tuple[str, float]:
        if self.monthly.empty:
            return ("—", 0.0)
        idx = self.monthly.idxmax()
        return (str(idx), float(self.monthly[idx]))

    @property
    def worst_month(self) -> tuple[str, float]:
        if self.monthly.empty:
            return ("—", 0.0)
        idx = self.monthly.idxmin()
        return (str(idx), float(self.monthly[idx]))

    @property
    def pct_positive_months(self) -> float:
        if self.monthly.empty:
            return 0.0
        return float((self.monthly > 0).mean())

    def summary_lines(self) -> list[str]:
        """Terminal-friendly one-line-per-metric summary."""
        calmar_str = f"{self.calmar:.2f}" if math.isfinite(self.calmar) else "∞"
        bm, bv = self.best_month
        wm, wv = self.worst_month
        return [
            f"  Total R            : {self.total_r:+.2f} R",
            f"  Annual avg R       : {self.annual_r:+.2f} R/yr",
            f"  Sharpe (monthly)   : {self.sharpe:.2f}",
            f"  Sortino (monthly)  : {self.sortino:.2f}",
            f"  Calmar             : {calmar_str}",
            f"  Max drawdown       : {self.max_dd:.2f} R",
            f"  Recovery           : {self.recovery_days or 'not yet recovered'} days",
            f"  Positive months    : {self.pct_positive_months:.0%}",
            f"  Best month         : {bm}  {bv:+.2f} R",
            f"  Worst month        : {wm}  {wv:+.2f} R",
        ]


# ── builder ───────────────────────────────────────────────────────────────────

def build_curve(trades: list["Trade"]) -> EquityCurve:
    """
    Build an EquityCurve from a list of closed Trade objects.

    Trades are sorted by exit_date.  Ties broken by entry_date.
    Open (unclosed) trades are silently ignored.
    """
    closed = [
        t for t in trades
        if t.exit_date is not None and t.r_multiple is not None
    ]
    if not closed:
        _empty = pd.Series(dtype=float)
        return EquityCurve(
            equity=_empty, drawdown=_empty,
            monthly=_empty, annual=_empty,
            sharpe=float("nan"), sortino=float("nan"),
            calmar=float("nan"), max_dd=0.0, recovery_days=None,
        )

    closed.sort(key=lambda t: (t.exit_date, t.entry_date))

    dates = pd.to_datetime([t.exit_date for t in closed])
    rs = np.array([float(t.effective_r) for t in closed])

    # ── equity curve ──────────────────────────────────────────────────────────
    equity_vals = np.cumsum(rs)
    equity = pd.Series(equity_vals, index=dates, name="equity_r")

    # ── drawdown curve ────────────────────────────────────────────────────────
    peak = np.maximum.accumulate(equity_vals)
    dd_vals = peak - equity_vals
    drawdown = pd.Series(dd_vals, index=dates, name="drawdown_r")

    max_dd = float(dd_vals.max())

    # ── recovery from max drawdown ────────────────────────────────────────────
    recovery_days: int | None = None
    if max_dd > 0:
        trough_idx = int(dd_vals.argmax())
        trough_pk = peak[trough_idx]
        # Find first bar after trough where equity exceeds the prior peak
        subsequent = np.where(
            (np.arange(len(equity_vals)) > trough_idx) &
            (equity_vals >= trough_pk)
        )[0]
        if len(subsequent):
            recovery_idx = subsequent[0]
            recovery_days = (dates[recovery_idx] - dates[trough_idx]).days

    # ── monthly aggregation ───────────────────────────────────────────────────
    month_keys = [f"{d.year:04d}-{d.month:02d}" for d in dates]
    monthly_dict: dict[str, float] = {}
    for key, r in zip(month_keys, rs):
        monthly_dict[key] = monthly_dict.get(key, 0.0) + r

    monthly = pd.Series(
        list(monthly_dict.values()),
        index=pd.Index(sorted(monthly_dict), name="month"),
        name="monthly_r",
    )

    # ── annual aggregation ────────────────────────────────────────────────────
    year_keys = [d.year for d in dates]
    annual_dict: dict[int, float] = {}
    for yr, r in zip(year_keys, rs):
        annual_dict[yr] = annual_dict.get(yr, 0.0) + r

    annual = pd.Series(
        list(annual_dict.values()),
        index=pd.Index(sorted(annual_dict), name="year"),
        name="annual_r",
    )

    # ── Sharpe / Sortino ──────────────────────────────────────────────────────
    monthly_vals = list(monthly_dict.values())
    sh = sharpe_ratio(monthly_vals)
    so = sortino_ratio(monthly_vals)

    # ── Calmar = annualised_r / max_dd ────────────────────────────────────────
    if max_dd > 0:
        n_years = max(
            (dates[-1] - dates[0]).days / 365.25, 1.0 / 12
        )
        ann_r = float(equity_vals[-1]) / n_years
        calmar = ann_r / max_dd
    else:
        calmar = float("inf")

    return EquityCurve(
        equity=equity,
        drawdown=drawdown,
        monthly=monthly,
        annual=annual,
        sharpe=sh,
        sortino=so,
        calmar=calmar,
        max_dd=max_dd,
        recovery_days=recovery_days,
    )


# ── per-ticker attribution ────────────────────────────────────────────────────

@dataclass
class TickerAttribution:
    ticker: str
    n_trades: int
    win_rate: float
    expectancy_r: float
    total_r: float
    best_r: float
    worst_r: float
    avg_bars: float


def attribution_table(trades: list["Trade"]) -> list[TickerAttribution]:
    """
    Group closed trades by ticker and compute per-ticker statistics.

    Returns a list sorted by total_r descending.
    """
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        if t.exit_date is not None and t.r_multiple is not None:
            groups[t.ticker].append(t)

    rows: list[TickerAttribution] = []
    for ticker, ts in groups.items():
        rs = [t.effective_r for t in ts]
        bars = [
            (t.exit_date - t.entry_date).days
            if t.exit_date and t.entry_date else 0
            for t in ts
        ]
        rows.append(TickerAttribution(
            ticker=ticker,
            n_trades=len(ts),
            win_rate=sum(1 for r in rs if r > 0) / len(rs),
            expectancy_r=float(np.mean(rs)),
            total_r=float(np.sum(rs)),
            best_r=float(max(rs)),
            worst_r=float(min(rs)),
            avg_bars=float(np.mean(bars)) if bars else 0.0,
        ))

    # Tie-break: when total_r ties, worst-trade ascending so chronic
    # losers float to the bottom even at the same headline R. Postmortem
    # "Report niceties" — see TODO.md.
    return sorted(rows, key=lambda x: (-x.total_r, x.worst_r))
