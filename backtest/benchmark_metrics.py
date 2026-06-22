"""
backtest/benchmark_metrics.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Benchmark-relative ("active-return") metrics — the excess analogue of the absolute
Sharpe/Sortino in ``stats_utils.py``: excess-Sharpe, Information Ratio, alpha/beta,
and % periods beating. Pure functions; ``scripts/benchmark_relative.py`` does the I/O.

UNIT CONTRACT (load-bearing — read before using)
─────────────────────────────────────────────────
Every metric here except :func:`align_strategy_benchmark` operates on two *already
aligned, same-unit* monthly series. Strategy P&L is in **R** (unit-less); a price
benchmark (SPY) is in **decimal return**. Each excess metric uses the difference
``strat − bench``, meaningful only when both legs share a unit — so the **caller**
must first convert strategy R → return under an explicit equity-per-R assumption
(project policy: 1R = 1% equity) before calling these.

A series' own standalone Sharpe (``stats_utils.sharpe_ratio``) is the only unit-free
benchmark comparison; the excess metrics here depend on the 1R↔equity factor and must
be labelled as such by the caller (see ``validation_prereg.md`` §P1-M). With
risk-free = 0, :func:`excess_sharpe` and :func:`information_ratio` are the SAME
quantity (annualised Sharpe of the active-return series) — the latter delegates to the
former; both names exist to match the validation-program spec.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from backtest.stats_utils import sharpe_ratio


# ── alignment ───────────────────────────────────────────────────────────────────

def _to_period_series(s) -> pd.Series:
    """Coerce a monthly series to a float Series indexed by ``Period('M')``.

    Accepts an index that is already a PeriodIndex, a ``'YYYY-MM'`` string index (as
    produced by ``EquityCurve.monthly``), or a DatetimeIndex of month-end stamps (as
    produced by ``close.resample('ME')``). Duplicate months are summed.
    """
    if not isinstance(s, pd.Series):
        s = pd.Series(s)
    idx = s.index
    if isinstance(idx, pd.PeriodIndex):
        per = idx.asfreq("M")
    elif isinstance(idx, pd.DatetimeIndex):
        per = idx.to_period("M")
    else:
        per = pd.PeriodIndex(pd.to_datetime(idx.astype(str)), freq="M")
    out = pd.Series(np.asarray(s.values, dtype=float), index=per)
    return out.groupby(level=0).sum().sort_index()


def align_strategy_benchmark(strat_monthly, bench_monthly):
    """Align a strategy monthly-R series with a benchmark monthly-return series.

    The strategy series is densified to **contiguous** calendar months over its own
    active span ``[first, last]``, zero-filling months with no closed trade (same logic
    ``equity_curve.build_curve`` uses for Sharpe — flat months are real, and a flat
    strategy month in which the benchmark moved is a genuine relative gain/loss). The
    densified strategy is then intersected with the benchmark's months (the benchmark
    always has a value, so it is not zero-filled).

    Returns ``(periods, strat_arr, bench_arr)`` — a ``PeriodIndex('M')`` and two aligned
    float ndarrays, sorted ascending. Empty inputs yield empty outputs.
    """
    strat = _to_period_series(strat_monthly)
    bench = _to_period_series(bench_monthly)
    if strat.empty or bench.empty:
        return pd.PeriodIndex([], freq="M"), np.array([]), np.array([])
    full = pd.period_range(strat.index.min(), strat.index.max(), freq="M")
    strat = strat.reindex(full, fill_value=0.0)
    common = strat.index.intersection(bench.index).sort_values()
    return common, strat.reindex(common).to_numpy(), bench.reindex(common).to_numpy()


def month_end_returns(close) -> pd.Series:
    """Monthly %-returns of a price series: month-end close → ``pct_change``.

    Same convention as ``scripts/benchmark_spy.py`` / ``benchmark_relative.py`` (so SPY
    monthly returns tie out across the validation tools). ``close`` must carry a
    DatetimeIndex. Indexed by month-end Timestamp.
    """
    c = pd.Series(close).dropna()
    monthly_close = c.resample("ME").last().dropna()
    return monthly_close.pct_change().dropna()


def benchmark_by_months(month_keys: Sequence[str], bench_monthly) -> np.ndarray:
    """Benchmark monthly-return aligned to an explicit ordered list of ``'YYYY-MM'`` keys.

    Pairs with :func:`align_monthly_matrix`, which keys the strategy matrix by the same
    ``'YYYY-MM'`` month strings: a SPY-relative active-return matrix is then
    ``α·strat_matrix − benchmark_by_months(months, spy)[:, None]`` (same-unit, §P2-M).
    Months absent from the benchmark return NaN so the caller can drop them. Returns a
    float ndarray the same length as ``month_keys``.
    """
    bench = _to_period_series(bench_monthly)
    lookup = {str(p): float(v) for p, v in bench.items()}   # str(Period('M')) == 'YYYY-MM'
    return np.array([lookup.get(str(m), float("nan")) for m in month_keys], dtype=float)


# ── active-return metrics (require same-unit aligned inputs) ─────────────────────

def excess_sharpe(strat: Sequence[float], bench: Sequence[float]) -> float:
    """Annualised Sharpe of the active-return series ``strat − bench`` (rf = 0).

    SAME-UNIT REQUIRED (see module docstring). Returns NaN if < 2 aligned points or the
    active-return series has zero variance.
    """
    a = np.asarray(strat, dtype=float)
    b = np.asarray(bench, dtype=float)
    n = min(len(a), len(b))
    if n < 2:
        return float("nan")
    return sharpe_ratio((a[:n] - b[:n]).tolist())


def information_ratio(strat: Sequence[float], bench: Sequence[float]) -> float:
    """Information ratio = annualised mean active return / tracking error (rf = 0).

    Identical to :func:`excess_sharpe` by construction; provided as a named alias to
    match the program spec. SAME-UNIT REQUIRED.
    """
    return excess_sharpe(strat, bench)


def alpha_beta(strat: Sequence[float], bench: Sequence[float]) -> tuple[float, float]:
    """``(alpha_monthly, beta)`` from an OLS degree-1 fit of ``strat`` on ``bench``.

    ASSUMPTION-DEPENDENT: regressing R on % bakes in the 1R↔equity factor, so this is
    only meaningful once the caller has put both legs in the same unit. ``alpha`` is the
    monthly intercept (input units); ``beta`` is the dimensionless slope. Returns
    ``(nan, nan)`` for < 2 points or a constant benchmark.
    """
    a = np.asarray(strat, dtype=float)
    b = np.asarray(bench, dtype=float)
    n = min(len(a), len(b))
    if n < 2 or np.allclose(b[:n], b[0]):
        return float("nan"), float("nan")
    beta, alpha = np.polyfit(b[:n], a[:n], 1)  # polyfit returns [slope, intercept]
    return float(alpha), float(beta)


def pct_periods_beating(strat: Sequence[float], bench: Sequence[float]) -> float:
    """Fraction of aligned months where ``strat > bench`` (0..1).

    SAME-UNIT REQUIRED — the per-month comparison depends on the 1R↔equity factor, so a
    caller reporting this must state the assumption. NaN if no aligned points.
    """
    a = np.asarray(strat, dtype=float)
    b = np.asarray(bench, dtype=float)
    n = min(len(a), len(b))
    if n == 0:
        return float("nan")
    a, b = a[:n], b[:n]
    finite = np.isfinite(a) & np.isfinite(b)   # exclude NaN months, like the sibling metrics
    if not finite.any():
        return float("nan")
    return float((a[finite] > b[finite]).mean())
