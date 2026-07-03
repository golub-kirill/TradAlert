"""Unit tests for the Phase-1 benchmark-relative metrics (backtest/benchmark_metrics.py)
and the pre-registered verdict gate (scripts/benchmark_relative.verdict_at).

These are pure-math / pure-logic checks — no backtest, no I/O — so they run in the
normal ``pytest tests/`` suite and guard the metrics that Phases 1–3 of the
honest-validation program depend on. The load-bearing claim under test is the §P1-M
units result: a series' *own* Sharpe is scale-invariant, but the active-return
(``strat − bench``) IR/excess-Sharpe is NOT, so the verdict is leverage-dependent.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "studies"))

from backtest.benchmark_metrics import (  # noqa: E402
    align_strategy_benchmark, alpha_beta, benchmark_by_months, excess_sharpe,
    information_ratio, month_end_returns, pct_periods_beating,
)
from backtest.multiple_testing import align_monthly_matrix  # noqa: E402
from backtest.stats_utils import sharpe_ratio  # noqa: E402
from benchmark_relative import verdict_at       # noqa: E402


# ── alignment ─────────────────────────────────────────────────────────────────

def test_align_densifies_and_intersects():
    """Gapped 'YYYY-MM' strat series → contiguous zero-filled within its own span,
    then intersected with the month-end-stamped benchmark."""
    strat = pd.Series([1.0, -0.5, 2.0],
                      index=pd.Index(["2001-01", "2001-03", "2001-06"], name="month"))
    bidx = pd.date_range("2000-12-31", "2001-12-31", freq="ME")
    bench = pd.Series(np.linspace(0.01, 0.02, len(bidx)), index=bidx)
    per, sA, bA = align_strategy_benchmark(strat, bench)
    assert [str(p) for p in per] == ["2001-01", "2001-02", "2001-03",
                                     "2001-04", "2001-05", "2001-06"]
    assert np.allclose(sA, [1.0, 0.0, -0.5, 0.0, 0.0, 2.0])  # internal gaps zero-filled
    assert len(bA) == 6                                      # benchmark intersected, not filled


def test_align_empty_inputs():
    per, sA, bA = align_strategy_benchmark(pd.Series(dtype=float), pd.Series(dtype=float))
    assert len(per) == 0 and len(sA) == 0 and len(bA) == 0


# ── metric identities ───────────────────────────────────────────────────────────

def test_excess_sharpe_equals_information_ratio_equals_sharpe_of_diff():
    a = np.array([0.01, 0.02, -0.01, 0.03, 0.00, 0.015])
    b = np.array([0.008, 0.012, 0.005, 0.02, 0.01, 0.011])
    assert excess_sharpe(a, b) == information_ratio(a, b)
    assert abs(excess_sharpe(a, b) - sharpe_ratio((a - b).tolist())) < 1e-12


def test_raw_mixed_unit_excess_is_degenerate():
    """§P1-M: sharpe(strat_R − SPY_%) on raw mixed units collapses to ≈ the strategy's
    OWN Sharpe (R-scale ≫ %-scale), and is nearer it than the properly-scaled IR is."""
    R = np.array([1.0, -0.5, 2.0, 0.3, -1.2, 0.8, 1.5, -0.7, 0.4, 1.1, -0.2, 0.6])
    pct = np.array([0.02, -0.01, 0.03, 0.005, -0.02, 0.015, 0.025,
                    -0.012, 0.008, 0.018, -0.004, 0.011])
    raw = excess_sharpe(R, pct)
    own = sharpe_ratio(R.tolist())
    ir1 = information_ratio(R * 0.01, pct)
    assert abs(raw - own) < abs(ir1 - own)


def test_information_ratio_moves_with_leverage():
    """§P1-M load-bearing: the difference-series IR is NOT scale-invariant — it changes
    with the assumed 1R↔equity factor (whereas own-Sharpe is an algebraic invariant)."""
    R = np.array([1.0, -0.5, 2.0, 0.3, -1.2, 0.8, 1.5, -0.7, 0.4, 1.1, -0.2, 0.6])
    pct = np.array([0.02, -0.01, 0.03, 0.005, -0.02, 0.015, 0.025,
                    -0.012, 0.008, 0.018, -0.004, 0.011])
    assert abs(information_ratio(R * 0.005, pct) - information_ratio(R * 0.02, pct)) > 1e-6


def test_alpha_beta_recovers_linear_relation():
    rng = np.random.default_rng(0)
    bb = rng.normal(0.01, 0.04, 200)
    aa = 0.003 + 0.7 * bb + rng.normal(0, 1e-7, 200)
    alpha, beta = alpha_beta(aa, bb)
    assert abs(alpha - 0.003) < 1e-4 and abs(beta - 0.7) < 1e-3


def test_alpha_beta_constant_benchmark_is_nan():
    assert all(np.isnan(alpha_beta(np.array([1.0, 2, 3]), np.array([0.5, 0.5, 0.5]))))


def test_pct_periods_beating_strict_and_nan_excluded():
    assert abs(pct_periods_beating(np.array([1, 0, 2, 3]),
                                   np.array([0, 1, 1, 5])) - 0.5) < 1e-12
    # NaN months are excluded (not counted as losses): 2/2 finite pairs beat → 1.0
    assert abs(pct_periods_beating(np.array([1.0, np.nan, 2.0]),
                                   np.array([0.5, 0.6, 0.7])) - 1.0) < 1e-12


def test_short_or_zero_variance_inputs_are_nan():
    assert np.isnan(excess_sharpe([0.01], [0.0]))             # < 2 points
    assert np.isnan(excess_sharpe([0.02, 0.02], [0.01, 0.01]))  # zero-variance active series


# ── SPY-relative helpers (Phase 2/3 — backtest/benchmark_metrics) ────────────────

def test_month_end_returns_month_over_month():
    """Daily price series → month-end-close pct_change; first month dropped (pct_change NaN)."""
    jan = pd.date_range("2001-01-01", "2001-01-31", freq="D")
    feb = pd.date_range("2001-02-01", "2001-02-28", freq="D")
    mar = pd.date_range("2001-03-01", "2001-03-31", freq="D")
    idx = jan.append(feb).append(mar)
    vals = ([100.0] * len(jan)) + ([110.0] * len(feb)) + ([121.0] * len(mar))
    r = month_end_returns(pd.Series(vals, index=idx))
    assert np.allclose(r.values, [0.10, 0.10])                       # 110/100, 121/110
    assert [str(p.to_period("M")) for p in r.index] == ["2001-02", "2001-03"]


def test_benchmark_by_months_aligns_and_nans_missing():
    bidx = pd.to_datetime(["2001-01-31", "2001-02-28", "2001-03-31"])
    bench = pd.Series([0.01, -0.02, 0.03], index=bidx)
    out = benchmark_by_months(["2001-01", "2001-02", "2001-04"], bench)
    assert np.allclose(out[:2], [0.01, -0.02])
    assert np.isnan(out[2])                                          # 2001-04 absent → NaN


def test_benchmark_by_months_pairs_with_align_monthly_matrix():
    """The SPY-relative active matrix = α·strat_matrix − benchmark_by_months(months)[:,None]."""
    s1 = pd.Series([1.0, 2.0], index=["2001-01", "2001-03"])
    s2 = pd.Series([0.5, -1.0], index=["2001-02", "2001-03"])
    matrix, months = align_monthly_matrix([s1, s2])
    assert months == ["2001-01", "2001-02", "2001-03"]
    bench = pd.Series([0.01, 0.02, 0.03],
                      index=pd.to_datetime(["2001-01-31", "2001-02-28", "2001-03-31"]))
    spy = benchmark_by_months(months, bench)
    assert np.allclose(spy, [0.01, 0.02, 0.03])
    active = 0.01 * matrix - spy[:, None]                            # same shape, broadcastable
    assert active.shape == matrix.shape


# ── pre-registered verdict gate ─────────────────────────────────────────────────

def _mkrows(ir_by_window, beat_full):
    rows, by = [], {}
    for lab, ir in ir_by_window.items():
        r = dict(label=lab, ir_band={0.01: ir},
                 beat_band={0.01: (beat_full if lab == "Full" else 0.5)})
        rows.append(r)
        by[lab] = r
    return rows, by


_W = {"Full": 0.40, "10y": 0.30, "5y": 0.20, "3y": 0.10, "1y": 0.05}


def test_verdict_pass():
    assert verdict_at(*_mkrows(_W, 0.55), 0.01)["verdict"] == "PASS"


def test_verdict_fail_takes_precedence_over_pass_when_both_recent_nonpositive():
    rows, by = _mkrows({**_W, "3y": -0.1, "1y": -0.2}, 0.55)
    assert verdict_at(rows, by, 0.01)["verdict"] == "FAIL"


def test_verdict_fail_when_full_ir_nonpositive():
    rows, by = _mkrows({**_W, "Full": -0.01}, 0.90)
    assert verdict_at(rows, by, 0.01)["verdict"] == "FAIL"


def test_verdict_marginal_when_full_ir_below_threshold():
    rows, by = _mkrows({**_W, "Full": 0.20}, 0.55)
    assert verdict_at(rows, by, 0.01)["verdict"] == "MARGINAL"


def test_verdict_marginal_when_beat_not_above_half():
    # IR(full) ≥ 0.30 and not a FAIL, but %-beat ≤ 50% → not PASS → MARGINAL
    assert verdict_at(*_mkrows(_W, 0.40), 0.01)["verdict"] == "MARGINAL"


def test_verdict_single_recent_negative_does_not_fail():
    # 3y ≤ 0 but 1y > 0 must NOT trigger the recent-FAIL (needs BOTH)
    rows, by = _mkrows({**_W, "3y": -0.1}, 0.55)
    assert verdict_at(rows, by, 0.01)["verdict"] == "PASS"
