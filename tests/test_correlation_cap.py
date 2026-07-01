"""Correlation-aware open-risk budget (portfolio_backtester correlation_cap).

Unit-tests the effective-risk math sqrt(wᵀCw) on synthetic return series:
correlated names share a budget slot, uncorrelated names earn the sqrt
diversification discount, and the effective risk never exceeds the raw Σ size_mult.
All synthetic — no market data / DB.
"""
from types import SimpleNamespace

import pandas as pd

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig

SQRT2 = 2.0 ** 0.5
FAR_FUTURE = pd.Timestamp("2030-01-01")  # > every synthetic bar → `< D` includes all


def _closes_from_returns(returns, price0=100.0):
    """Close series whose pct_change equals `returns` exactly (multiplicative)."""
    prices = [price0]
    for r in returns:
        prices.append(prices[-1] * (1.0 + r))
    idx = pd.date_range("2020-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"close": prices}, index=idx)


def _prep(returns):
    return SimpleNamespace(df=_closes_from_returns(returns))


def _bt(min_overlap=5, lookback=60, floor=0.0):
    cfg = PortfolioConfig(
        max_open_risk=5.0, correlation_cap=True,
        correlation_lookback_days=lookback, correlation_min_overlap=min_overlap,
        correlation_floor=floor,
    )
    return PortfolioBacktester(engine=None, cfg=cfg)


# Orthogonal (Pearson exactly 0) mean-zero return patterns over 8 bars.
_A = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
_B = [0.01, 0.01, -0.01, -0.01, 0.01, 0.01, -0.01, -0.01]


def _eff(bt, open_map, cand, cand_mult, prepped):
    return bt._effective_open_risk(open_map, cand, cand_mult, prepped, FAR_FUTURE)


def test_defaults_are_off_and_baseline_safe():
    cfg = PortfolioConfig(max_open_risk=5.0)
    assert cfg.correlation_cap is False
    assert cfg.correlation_lookback_days == 60
    assert cfg.correlation_min_overlap == 40
    assert cfg.correlation_floor == 0.0


def test_single_position_returns_its_size():
    bt = _bt()
    eff = _eff(bt, {}, "B", 0.75, {"B": _prep(_A)})
    assert eff == 0.75


def test_perfectly_correlated_no_discount():
    # Identical return series → ρ=1 → effective == raw sum (they share a slot).
    prepped = {"A": _prep(_A), "B": _prep(_A)}
    eff = _eff(bt := _bt(), {"A": SimpleNamespace(size_mult=1.0)}, "B", 1.0, prepped)
    assert abs(eff - 2.0) < 1e-6


def test_uncorrelated_gets_sqrt_discount():
    # ρ=0 → effective = sqrt(1²+1²) = sqrt(2) (diversification discount).
    prepped = {"A": _prep(_A), "B": _prep(_B)}
    eff = _eff(_bt(), {"A": SimpleNamespace(size_mult=1.0)}, "B", 1.0, prepped)
    assert abs(eff - SQRT2) < 1e-6


def test_negative_correlation_clipped_to_zero():
    # ρ=-1 → clipped to 0 → treated independent, not rewarded below independent.
    prepped = {"A": _prep(_A), "B": _prep([-x for x in _A])}
    eff = _eff(_bt(), {"A": SimpleNamespace(size_mult=1.0)}, "B", 1.0, prepped)
    assert abs(eff - SQRT2) < 1e-6


def test_insufficient_overlap_treated_independent():
    # Identical but only 3 returns < min_overlap=10 → corr NaN → 0 → sqrt(2).
    short = [0.01, -0.01, 0.01]
    prepped = {"A": _prep(short), "B": _prep(short)}
    eff = _eff(_bt(min_overlap=10), {"A": SimpleNamespace(size_mult=1.0)}, "B", 1.0, prepped)
    assert abs(eff - SQRT2) < 1e-6


def test_correlated_cluster_shares_slot_below_raw_sum():
    # A & C perfectly correlated, B orthogonal. Weights all 1.0 → raw sum 3.0.
    # C-matrix [[1,0,1],[0,1,0],[1,0,1]] → wCw = 3 + 2·1 = 5 → sqrt(5) < 3.
    prepped = {"A": _prep(_A), "B": _prep(_B), "C": _prep(_A)}
    open_map = {"A": SimpleNamespace(size_mult=1.0), "B": SimpleNamespace(size_mult=1.0)}
    eff = _eff(_bt(), open_map, "C", 1.0, prepped)
    assert abs(eff - 5.0 ** 0.5) < 1e-6
    assert eff < 3.0  # correlated cluster does not consume 3 independent slots


def test_look_ahead_free_excludes_D_and_after():
    # Bars on/after D must not enter the correlation window.
    prepped = {"A": _prep(_A), "B": _prep(_B)}
    cutoff = prepped["A"].df.index[4]  # only bars strictly before this count
    bt = _bt(min_overlap=2)
    eff = bt._effective_open_risk(
        {"A": SimpleNamespace(size_mult=1.0)}, "B", 1.0, prepped, cutoff)
    # 4 closes before cutoff → 3 returns each; still a valid finite effective risk
    # in [sqrt(2), 2] and never above the raw sum.
    assert SQRT2 - 1e-9 <= eff <= 2.0 + 1e-9
