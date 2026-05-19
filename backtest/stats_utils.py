"""
backtest/stats_utils.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pure-math statistics for the TradAlert backtesting system.

All functions operate on plain Python lists / numpy arrays — no
dependency on the backtest infrastructure.  Import anywhere.

Public API
──────────
    bootstrap_ci(r_multiples, metric, n, ci)  → BootstrapResult
    kelly_fraction(win_rate, avg_win_r, avg_loss_r)  → KellyResult
    sharpe_ratio(monthly_r, risk_free_annual)  → float
    sortino_ratio(monthly_r, risk_free_annual)  → float
    consecutive_loss_stats(r_multiples)  → ConsecutiveLossStats
    drawdown_series(r_multiples)  → np.ndarray   (running drawdown in R)
    max_drawdown(r_multiples)  → float
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np


# ── result types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BootstrapResult:
    """
    Bootstrap confidence interval for a backtest metric.

    Attributes
    ----------
    estimate   : Point estimate from the observed sample.
    lower      : Lower bound of the CI.
    upper      : Upper bound of the CI.
    ci         : Confidence level, e.g. 0.95.
    n_samples  : Number of bootstrap resamples used.
    std_error  : Bootstrap standard error of the statistic.
    """
    estimate: float
    lower: float
    upper: float
    ci: float
    n_samples: int
    std_error: float

    def __str__(self) -> str:
        pct = int(self.ci * 100)
        return (
            f"{self.estimate:+.3f}  "
            f"[{pct}% CI  {self.lower:+.3f} … {self.upper:+.3f}]  "
            f"SE={self.std_error:.3f}"
        )

    @property
    def significant(self) -> bool:
        """True when the CI excludes zero (i.e. statistically meaningful)."""
        return self.lower > 0 or self.upper < 0


@dataclass(frozen=True)
class KellyResult:
    """
    Kelly fraction analysis for a fixed-R trading strategy.

    Attributes
    ----------
    full_kelly      : Theoretically optimal fraction of bankroll per trade.
    half_kelly      : Conservative recommendation (full / 2).
    quarter_kelly   : Ultra-conservative (full / 4).
    win_rate        : Input win-rate.
    avg_win_r       : Input average winner in R.
    avg_loss_r      : Input average loser in R (positive convention).
    edge_per_trade  : Expected R per trade = WR*avg_win − (1−WR)*avg_loss.
    breakeven_wr    : Win-rate at which Kelly = 0 (edge = 0).
    """
    full_kelly: float
    half_kelly: float
    quarter_kelly: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    edge_per_trade: float
    breakeven_wr: float

    def dollar_risk(self, bankroll: float, fraction: str = "half") -> float:
        """Dollar amount to risk per trade given a bankroll and Kelly variant."""
        k = {"full": self.full_kelly, "half": self.half_kelly,
             "quarter": self.quarter_kelly}[fraction]
        return bankroll * max(k, 0.0)

    def __str__(self) -> str:
        return (
            f"Kelly: full={self.full_kelly:.1%}  "
            f"half={self.half_kelly:.1%}  "
            f"quarter={self.quarter_kelly:.1%}  "
            f"edge/trade={self.edge_per_trade:+.3f}R  "
            f"breakeven WR={self.breakeven_wr:.1%}"
        )


@dataclass(frozen=True)
class ConsecutiveLossStats:
    """
    Run-length statistics on losing trades.

    Attributes
    ----------
    max_consecutive  : Longest losing streak observed.
    avg_consecutive  : Average streak length.
    p_streak_n       : Probability (empirical) of a streak ≥ 5 losses.
    prob_n_losses    : Dict mapping streak length → analytical binomial P.
    """
    max_consecutive: int
    avg_consecutive: float
    streaks: list[int]  # all observed losing run lengths

    @property
    def p_streak_5(self) -> float:
        """Fraction of streaks that reached ≥ 5 consecutive losses."""
        if not self.streaks:
            return 0.0
        return sum(1 for s in self.streaks if s >= 5) / len(self.streaks)

    def binomial_p(self, win_rate: float, n: int) -> float:
        """Probability of exactly n consecutive losses: (1-WR)^n."""
        return (1.0 - win_rate) ** n

    def binomial_at_least(self, win_rate: float, n: int,
                          sequence_length: int) -> float:
        """
        Approximate probability of seeing at least one streak of ≥ n losses
        in a sequence of ``sequence_length`` trades.
        Uses the runs-in-Bernoulli-sequence approximation.
        """
        p_loss = 1.0 - win_rate
        p_block = p_loss ** n
        # Expected number of starting positions for a run of length n
        expected_starts = (sequence_length - n + 1) * p_block
        # P(at least one) ≈ 1 - e^(-λ) for rare events
        return 1.0 - math.exp(-expected_starts)

    def __str__(self) -> str:
        return (
            f"Max consecutive losses: {self.max_consecutive}  "
            f"Avg streak: {self.avg_consecutive:.1f}  "
            f"P(streak≥5): {self.p_streak_5:.1%}"
        )


# ── bootstrap CI ─────────────────────────────────────────────────────────────

_METRIC_FN: dict[str, Callable[[np.ndarray], float]] = {
    "expectancy": lambda r: float(r.mean()),
    "win_rate": lambda r: float((r > 0).mean()),
    "total_r": lambda r: float(r.sum()),
    "median_r": lambda r: float(np.median(r)),
    "profit_factor": lambda r: (
        float(r[r > 0].sum() / -r[r < 0].sum())
        if (r < 0).any() and (r > 0).any() else float("inf")
    ),
}


def bootstrap_ci(
        r_multiples: Sequence[float],
        metric: str = "expectancy",
        n: int = 10_000,
        ci: float = 0.95,
        seed: int = 42,
) -> BootstrapResult:
    """
    Non-parametric bootstrap confidence interval.

    Parameters
    ----------
    r_multiples : Observed R-multiples (one per trade).
    metric      : One of "expectancy", "win_rate", "total_r",
                  "median_r", "profit_factor".
    n           : Number of bootstrap resamples (default 10 000).
    ci          : Confidence level (default 0.95 → 95% CI).
    seed        : RNG seed for reproducibility.

    Returns
    -------
    BootstrapResult
    """
    if metric not in _METRIC_FN:
        raise ValueError(
            f"Unknown metric '{metric}'. "
            f"Choose from: {list(_METRIC_FN)}"
        )
    arr = np.array(r_multiples, dtype=float)
    fn = _METRIC_FN[metric]
    est = fn(arr)

    if len(arr) < 2:
        return BootstrapResult(
            estimate=est, lower=est, upper=est,
            ci=ci, n_samples=0, std_error=0.0,
        )

    rng = np.random.default_rng(seed)
    boots = np.array([
        fn(rng.choice(arr, size=len(arr), replace=True))
        for _ in range(n)
    ])

    alpha = (1.0 - ci) / 2
    lower = float(np.percentile(boots, alpha * 100))
    upper = float(np.percentile(boots, (1 - alpha) * 100))
    se = float(boots.std())

    return BootstrapResult(
        estimate=est,
        lower=lower,
        upper=upper,
        ci=ci,
        n_samples=n,
        std_error=se,
    )


def bootstrap_all(
        r_multiples: Sequence[float],
        n: int = 10_000,
        ci: float = 0.95,
        seed: int = 42,
) -> dict[str, BootstrapResult]:
    """
    Run bootstrap CI for all standard metrics at once.

    Returns dict keyed by metric name.
    """
    return {
        m: bootstrap_ci(r_multiples, m, n, ci, seed)
        for m in _METRIC_FN
    }


# ── Kelly fraction ────────────────────────────────────────────────────────────

def kelly_fraction(
        win_rate: float,
        avg_win_r: float,
        avg_loss_r: float,
) -> KellyResult:
    """
    Kelly criterion for a fixed-R trading strategy.

    The generalised Kelly formula for a bet with asymmetric payoffs:

        K = (WR / loss_r) - ((1 - WR) / win_r)

    which simplifies to the standard K = WR - (1-WR)/R for fixed R:R.

    Parameters
    ----------
    win_rate   : Fraction of winning trades [0, 1].
    avg_win_r  : Average R-multiple of winning trades (positive).
    avg_loss_r : Average R-multiple of losing trades (positive convention,
                 e.g. 1.05 means the average loss is 1.05 R).

    Returns
    -------
    KellyResult
    """
    win_rate = float(win_rate)
    avg_win_r = float(avg_win_r)
    avg_loss_r = float(avg_loss_r)

    loss_rate = 1.0 - win_rate

    # Edge per trade
    edge = win_rate * avg_win_r - loss_rate * avg_loss_r

    # Generalised Kelly
    if avg_win_r > 0 and avg_loss_r > 0:
        full_kelly = (win_rate / avg_loss_r) - (loss_rate / avg_win_r)
    else:
        full_kelly = 0.0

    full_kelly = max(full_kelly, 0.0)  # floor at zero (no negative sizing)

    # Breakeven win rate: WR at which edge = 0
    # WR * avg_win = (1 - WR) * avg_loss
    # WR = avg_loss / (avg_win + avg_loss)
    if avg_win_r + avg_loss_r > 0:
        bkeven = avg_loss_r / (avg_win_r + avg_loss_r)
    else:
        bkeven = float("nan")

    return KellyResult(
        full_kelly=full_kelly,
        half_kelly=full_kelly / 2,
        quarter_kelly=full_kelly / 4,
        win_rate=win_rate,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        edge_per_trade=edge,
        breakeven_wr=bkeven,
    )


# ── Sharpe / Sortino ──────────────────────────────────────────────────────────

def sharpe_ratio(
        monthly_r: Sequence[float],
        risk_free_annual: float = 0.05,
) -> float:
    """
    Annualised Sharpe ratio from a monthly R-multiple series.

    Uses excess return over a risk-free rate expressed in R units.
    With typical Kelly half-fraction sizing (~10% of equity per 1R),
    1R ≈ 10% return, so a 5% annual risk-free ≈ 0.042 R/month.

    Returns NaN when std == 0.
    """
    arr = np.array(monthly_r, dtype=float)
    if len(arr) < 2:
        return float("nan")

    rf_monthly = (1 + risk_free_annual) ** (1 / 12) - 1
    # Express rf in R units: assume 1R ≈ 10% return (half-Kelly context)
    rf_r = rf_monthly / 0.10
    excess = arr - rf_r
    std = excess.std(ddof=1)
    if std == 0:
        return float("nan")
    return float(excess.mean() / std * math.sqrt(12))


def sortino_ratio(
        monthly_r: Sequence[float],
        risk_free_annual: float = 0.05,
) -> float:
    """
    Annualised Sortino ratio — penalises only downside deviation.

    Returns NaN when there are no negative months.
    """
    arr = np.array(monthly_r, dtype=float)
    if len(arr) < 2:
        return float("nan")

    rf_monthly = (1 + risk_free_annual) ** (1 / 12) - 1
    rf_r = rf_monthly / 0.10
    excess = arr - rf_r
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    dd_std = math.sqrt((downside ** 2).mean())
    if dd_std == 0:
        return float("nan")
    return float(excess.mean() / dd_std * math.sqrt(12))


# ── Drawdown ──────────────────────────────────────────────────────────────────

def drawdown_series(r_multiples: Sequence[float]) -> np.ndarray:
    """
    Running drawdown from equity peak, in R units.

    Positive values → underwater (loss from peak).
    Returns an array the same length as r_multiples.
    """
    equity = np.cumsum(np.array(r_multiples, dtype=float))
    peak = np.maximum.accumulate(equity)
    return peak - equity  # always ≥ 0


def max_drawdown(r_multiples: Sequence[float]) -> float:
    """Peak-to-trough drawdown in R units."""
    dd = drawdown_series(r_multiples)
    return float(dd.max()) if len(dd) else 0.0


# ── Consecutive loss streaks ──────────────────────────────────────────────────

def consecutive_loss_stats(r_multiples: Sequence[float]) -> ConsecutiveLossStats:
    """
    Analyse runs of consecutive losing trades.

    A trade is a loss when its R-multiple ≤ 0.

    Returns ConsecutiveLossStats with observed streak lengths,
    max streak, and average streak.
    """
    streaks: list[int] = []
    current = 0

    for r in r_multiples:
        if r <= 0:
            current += 1
        else:
            if current > 0:
                streaks.append(current)
            current = 0
    if current > 0:
        streaks.append(current)

    if not streaks:
        return ConsecutiveLossStats(
            max_consecutive=0, avg_consecutive=0.0, streaks=[],
        )

    return ConsecutiveLossStats(
        max_consecutive=max(streaks),
        avg_consecutive=sum(streaks) / len(streaks),
        streaks=streaks,
    )


# ── Monthly P&L series ────────────────────────────────────────────────────────

def monthly_r_series(
        exit_dates: Sequence,  # datetime.date or pd.Timestamp
        r_multiples: Sequence[float],
) -> "dict[str, float]":
    """
    Aggregate R-multiples into a dict keyed by "YYYY-MM" strings.

    Parameters
    ----------
    exit_dates  : Sequence of exit dates (one per trade).
    r_multiples : Corresponding R-multiples.

    Returns
    -------
    Dict mapping "YYYY-MM" → total R for that month (sorted chronologically).
    """
    from collections import defaultdict
    monthly: dict[str, float] = defaultdict(float)
    for d, r in zip(exit_dates, r_multiples):
        key = f"{d.year:04d}-{d.month:02d}"
        monthly[key] += r
    return dict(sorted(monthly.items()))
