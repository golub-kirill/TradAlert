"""
Edge-case fixes in stats_utils (audit E1, E2):
  - bootstrap_ci("profit_factor") must not emit nan bounds/SE when some
    resamples have zero losers (profit_factor -> +inf);
  - consecutive_loss_stats treats a scratch (r == 0) as neutral, not a loss.
"""

from __future__ import annotations

import math

from backtest.stats_utils import bootstrap_ci, consecutive_loss_stats


def test_bootstrap_profit_factor_no_nan_with_rare_losses():
    rs = [2.0, 1.0, 1.5, 0.8, -0.2]  # rare loss -> many resamples have no losers
    res = bootstrap_ci(rs, metric="profit_factor", n=2000, seed=42)
    assert math.isfinite(res.lower)
    assert math.isfinite(res.upper)
    assert math.isfinite(res.std_error)
    assert 0 < res.n_samples <= 2000  # some inf resamples dropped


def test_bootstrap_finite_metric_drops_nothing():
    rs = [0.5, -1.0, 1.2, -0.3, 0.8, -0.5, 1.5]
    res = bootstrap_ci(rs, metric="expectancy", n=2000, seed=42)
    assert math.isfinite(res.lower) and math.isfinite(res.upper)
    assert res.n_samples == 2000


def test_consecutive_losses_scratch_breaks_streak():
    # r == 0 must NOT extend a losing streak.
    s = consecutive_loss_stats([-1.0, 0.0, -1.0])
    assert s.max_consecutive == 1
    assert s.streaks == [1, 1]

    # Genuine consecutive losses still streak.
    s2 = consecutive_loss_stats([-1.0, -1.0, -0.5, 1.0, -2.0])
    assert s2.max_consecutive == 3
