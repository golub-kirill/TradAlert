"""
Unit tests for backtest/multiple_testing.py.

All tests run on synthetic arrays / seeded RNG — no SweepEngine, no universe
load — so the suite stays fast. Reference values are hand-derived from the
closed-form definitions (no scipy dependency, matching the project's house
style of validating math against independent references).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from backtest.multiple_testing import (
    PBOResult,
    PSRResult,
    DSRResult,
    RealityCheckResult,
    _kurtosis,
    _skew,
    _stationary_bootstrap_indices,
    align_monthly_matrix,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    whites_reality_check,
)


# ── normal CDF / inverse CDF ────────────────────────────────────────────────────

def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-12)
    assert norm_cdf(1.0) == pytest.approx(0.8413447460685429, abs=1e-9)
    assert norm_cdf(-1.0) == pytest.approx(0.15865525393145707, abs=1e-9)
    assert norm_cdf(1.959963984540054) == pytest.approx(0.975, abs=1e-9)


def test_norm_cdf_symmetry():
    for x in (0.3, 1.0, 2.5, 3.7):
        assert norm_cdf(-x) == pytest.approx(1.0 - norm_cdf(x), abs=1e-12)


def test_norm_ppf_known_values():
    assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert norm_ppf(0.975) == pytest.approx(1.959963984540054, abs=1e-7)
    assert norm_ppf(0.95) == pytest.approx(1.6448536269514722, abs=1e-7)


def test_norm_ppf_inverse_of_cdf():
    for x in np.linspace(-3.5, 3.5, 29):
        assert norm_ppf(norm_cdf(x)) == pytest.approx(x, abs=1e-7)


def test_norm_ppf_domain():
    with pytest.raises(ValueError):
        norm_ppf(0.0)
    with pytest.raises(ValueError):
        norm_ppf(1.0)
    with pytest.raises(ValueError):
        norm_ppf(-0.1)


# ── moments ─────────────────────────────────────────────────────────────────────

def test_skew_symmetric_is_zero():
    assert _skew(np.array([-2.0, -1.0, 0.0, 1.0, 2.0])) == pytest.approx(0.0, abs=1e-12)
    # shift-invariant: skew unchanged by adding a constant
    assert _skew(np.array([8.0, 9.0, 10.0, 11.0, 12.0])) == pytest.approx(0.0, abs=1e-12)


def test_kurtosis_known_value_and_non_excess():
    # [-2,-1,0,1,2]: m2 = 2.0, m4 = 6.8 → kurt = 6.8 / 4 = 1.7 (NON-excess)
    assert _kurtosis(np.array([-2.0, -1.0, 0.0, 1.0, 2.0])) == pytest.approx(1.7, abs=1e-12)
    # a (roughly) normal sample has non-excess kurtosis near 3
    rng = np.random.default_rng(0)
    big_normal = rng.standard_normal(200_000)
    assert _kurtosis(big_normal) == pytest.approx(3.0, abs=0.1)


def test_moments_degenerate_guards():
    assert _skew(np.array([1.0, 2.0])) == 0.0      # < 3 elements
    assert _kurtosis(np.array([1.0, 2.0, 3.0])) == 3.0  # < 4 elements
    assert _skew(np.array([5.0, 5.0, 5.0, 5.0])) == 0.0  # zero variance
    assert _kurtosis(np.array([5.0, 5.0, 5.0, 5.0])) == 3.0


# ── Probabilistic Sharpe Ratio ──────────────────────────────────────────────────

def test_psr_symmetric_zero_mean_is_half():
    res = probabilistic_sharpe_ratio([-2.0, -1.0, 0.0, 1.0, 2.0], 0.0)
    assert isinstance(res, PSRResult)
    assert res.sr_hat == pytest.approx(0.0, abs=1e-12)
    assert res.psr == pytest.approx(0.5, abs=1e-9)


def test_psr_shifted_symmetric_closed_form():
    # [-2..2] + 1 → mean=1, std(ddof=1)=√2.5, skew=0, kurt=1.7
    arr = [-1.0, 0.0, 1.0, 2.0, 3.0]
    res = probabilistic_sharpe_ratio(arr, 0.0)
    sr_hat = 1.0 / math.sqrt(2.5)
    assert res.skew == pytest.approx(0.0, abs=1e-12)
    assert res.kurtosis == pytest.approx(1.7, abs=1e-12)
    assert res.sr_hat == pytest.approx(sr_hat, rel=1e-12)
    # denom = 1 - 0 + ((1.7-1)/4)·sr_hat²  ; z = sr_hat·√(T-1)/√denom
    denom = 1.0 + 0.175 * sr_hat ** 2
    z = sr_hat * math.sqrt(len(arr) - 1) / math.sqrt(denom)
    assert res.psr == pytest.approx(norm_cdf(z), rel=1e-12)


def test_psr_increases_with_mean():
    base = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
    low = probabilistic_sharpe_ratio(base + 0.5, 0.0).psr
    high = probabilistic_sharpe_ratio(base + 1.5, 0.0).psr
    assert 0.5 < low < high < 1.0


def test_psr_decreases_with_benchmark():
    arr = [-1.0, 0.0, 1.0, 2.0, 3.0]
    assert probabilistic_sharpe_ratio(arr, 0.0).psr > probabilistic_sharpe_ratio(arr, 0.5).psr


def test_psr_increases_with_more_periods():
    pattern = [-2.0, -1.0, 0.0, 1.0, 2.0, 4.0]  # positive mean
    short = probabilistic_sharpe_ratio(pattern, 0.0).psr
    long = probabilistic_sharpe_ratio(pattern * 6, 0.0).psr  # same shape, larger T
    assert long > short


def test_psr_too_few_periods_is_nan():
    assert math.isnan(probabilistic_sharpe_ratio([1.0], 0.0).psr)
    assert math.isnan(probabilistic_sharpe_ratio([5.0, 5.0, 5.0], 0.0).psr)  # zero std


# ── expected maximum Sharpe ─────────────────────────────────────────────────────

def test_expected_max_sharpe_monotonic_in_n():
    vals = [expected_max_sharpe(1.0, n) for n in (2, 10, 100, 1000)]
    assert all(a < b for a, b in zip(vals, vals[1:]))
    assert all(v > 0 for v in vals)


def test_expected_max_sharpe_scales_with_sqrt_variance():
    assert expected_max_sharpe(4.0, 50) == pytest.approx(2.0 * expected_max_sharpe(1.0, 50), rel=1e-12)


def test_expected_max_sharpe_guards():
    assert expected_max_sharpe(1.0, 1) == 0.0
    assert expected_max_sharpe(0.0, 100) == 0.0
    assert expected_max_sharpe(-1.0, 100) == 0.0


# ── Deflated Sharpe Ratio ───────────────────────────────────────────────────────

def test_dsr_le_psr_zero():
    selected = [-1.0, 0.0, 1.0, 2.0, 3.0]
    sharpes = [0.1, 0.2, 0.3, 0.4, 0.63]  # variance > 0
    res = deflated_sharpe_ratio(selected, sharpes)
    assert isinstance(res, DSRResult)
    assert res.sr0 > 0.0
    assert res.dsr <= res.psr_vs_zero


def test_dsr_equals_psr_when_no_search_inflation():
    selected = [-1.0, 0.0, 1.0, 2.0, 3.0]
    # single trial → variance undefined → SR0 = 0 → DSR == PSR(0)
    res = deflated_sharpe_ratio(selected, [0.5], n_trials=1)
    assert res.sr0 == 0.0
    assert res.dsr == pytest.approx(res.psr_vs_zero, rel=1e-12)


def test_dsr_more_trials_lowers_dsr():
    selected = [-1.0, 0.0, 1.0, 2.0, 3.0]
    sharpes = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.63]
    few = deflated_sharpe_ratio(selected, sharpes, n_trials=3)
    many = deflated_sharpe_ratio(selected, sharpes, n_trials=500)
    assert many.sr0 > few.sr0
    assert many.dsr <= few.dsr


def test_dsr_drops_non_finite_sharpes():
    selected = [-1.0, 0.0, 1.0, 2.0, 3.0]
    with_nans = [0.1, float("nan"), 0.3, None, 0.63]
    res = deflated_sharpe_ratio(selected, with_nans)
    assert res.n_trials == 3  # nan + None dropped


# ── monthly-R alignment ─────────────────────────────────────────────────────────

def test_align_monthly_matrix_union_and_zero_fill():
    a = {"2020-01": 1.0, "2020-03": 2.0}
    b = {"2020-02": 3.0, "2020-03": 4.0}
    mat, months = align_monthly_matrix([a, b])
    assert months == ["2020-01", "2020-02", "2020-03"]
    assert mat.shape == (3, 2)
    assert list(mat[:, 0]) == [1.0, 0.0, 2.0]
    assert list(mat[:, 1]) == [0.0, 3.0, 4.0]


def test_align_monthly_matrix_empty():
    mat, months = align_monthly_matrix([])
    assert mat.shape == (0, 0)
    assert months == []


# ── White's Reality Check ───────────────────────────────────────────────────────

def _noise_matrix(t: int, k: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal((t, k))


def test_reality_check_high_p_on_pure_noise():
    mat = _noise_matrix(120, 20, seed=1)
    res = whites_reality_check(mat, n_bootstrap=2000, seed=42)
    assert isinstance(res, RealityCheckResult)
    assert res.p_value > 0.05  # no real edge → not significant


def test_reality_check_low_p_on_planted_edge():
    mat = _noise_matrix(120, 20, seed=1)
    mat[:, 7] += 0.6  # strong, persistent edge in column 7
    res = whites_reality_check(mat, n_bootstrap=2000, seed=42)
    assert res.best_config_idx == 7
    assert res.p_value < 0.02


def test_reality_check_reproducible_with_seed():
    mat = _noise_matrix(80, 10, seed=3)
    r1 = whites_reality_check(mat, n_bootstrap=1000, seed=7)
    r2 = whites_reality_check(mat, n_bootstrap=1000, seed=7)
    assert r1.p_value == r2.p_value
    assert r1.observed_stat == r2.observed_stat


def test_reality_check_degenerate_matrix():
    assert math.isnan(whites_reality_check(np.zeros((1, 5))).p_value)   # < 2 rows
    assert math.isnan(whites_reality_check(np.zeros((10, 0))).p_value)  # 0 cols


def test_stationary_bootstrap_indices_in_range():
    rng = np.random.default_rng(0)
    idx = _stationary_bootstrap_indices(50, 6.0, 50, rng)
    assert idx.shape == (50,)
    assert idx.min() >= 0 and idx.max() < 50


def test_stationary_bootstrap_preserves_block_structure():
    # The whole point of the stationary bootstrap is contiguous blocks (cur+1 mod n
    # between geometric restarts). A large mean_block -> tiny restart prob -> almost
    # every step is a +1 wrap. An iid mutation (cur = randint each step) destroys
    # that, so requiring most steps be contiguous turns such a mutation red.
    rng = np.random.default_rng(0)
    n, size = 50, 400
    idx = _stationary_bootstrap_indices(n, 1000.0, size, rng)
    contiguous = sum(1 for t in range(1, size) if idx[t] == (idx[t - 1] + 1) % n)
    assert contiguous / (size - 1) > 0.8   # iid would be ≈ 1/n = 0.02


# ── Probability of Backtest Overfitting (CSCV) ──────────────────────────────────

def test_pbo_dominant_config_generalises_to_zero():
    # One column is strictly better every period → IS-best is always OOS-best →
    # it is never below the OOS median → PBO = 0.
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1.0, size=(120, 9))
    signal = rng.normal(0.5, 1.0, size=(120, 1)) + 3.0   # dominant column
    mat = np.hstack([noise, signal])
    res = probability_of_backtest_overfitting(mat, n_splits=8)
    assert isinstance(res, PBOResult)
    assert res.pbo == pytest.approx(0.0)
    assert res.median_logit > 0            # IS-best beats the OOS median


def test_pbo_pure_noise_is_near_one_half():
    # All columns iid noise → IS rank carries no OOS information → PBO ≈ 0.5.
    rng = np.random.default_rng(7)
    mat = rng.normal(0.0, 1.0, size=(160, 20))
    res = probability_of_backtest_overfitting(mat, n_splits=10)
    assert 0.35 < res.pbo < 0.65           # indistinguishable from overfit noise


def test_pbo_constructed_overfit_is_high():
    # Antisymmetric construction: each config is engineered to win one time-half
    # and lose the other, so whichever looks best IS is worst OOS → PBO → 1.
    T, K = 40, 6
    mat = np.zeros((T, K))
    half = T // 2
    for j in range(K):
        mat[:half, j] = (j + 1)                    # distinct positive means IS(first half)
        mat[half:, j] = -(j + 1)                   # inverted OOS(second half)
    # add tiny noise so std > 0
    mat += np.random.default_rng(3).normal(0, 1e-6, size=(T, K))
    res = probability_of_backtest_overfitting(mat, n_splits=4, embargo=0)
    assert res.pbo > 0.9


def test_pbo_is_deterministic():
    rng = np.random.default_rng(11)
    mat = rng.normal(0, 1, size=(96, 12))
    a = probability_of_backtest_overfitting(mat, n_splits=8)
    b = probability_of_backtest_overfitting(mat, n_splits=8)
    assert a.pbo == b.pbo and a.median_logit == b.median_logit


def test_pbo_forces_even_split_count():
    rng = np.random.default_rng(5)
    mat = rng.normal(0, 1, size=(90, 8))
    res = probability_of_backtest_overfitting(mat, n_splits=9)   # → 8
    assert res.n_splits == 8


def test_pbo_degenerate_returns_nan():
    # Fewer rows than splits, and single-column matrix, both → NaN.
    assert math.isnan(probability_of_backtest_overfitting(
        np.ones((4, 5)), n_splits=8).pbo)
    assert math.isnan(probability_of_backtest_overfitting(
        np.random.default_rng(0).normal(0, 1, (100, 1)), n_splits=8).pbo)


def test_pbo_combination_count_matches_choose():
    # S=8 → C(8,4) = 70 symmetric splits evaluated.
    rng = np.random.default_rng(2)
    mat = rng.normal(0, 1, size=(80, 6))
    res = probability_of_backtest_overfitting(mat, n_splits=8, embargo=0)
    assert res.n_combinations == math.comb(8, 4)
