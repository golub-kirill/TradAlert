"""Unit tests for scripts/benchmark_spy.py pure-math helpers (synthetic data)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import benchmark_spy as bs  # noqa: E402


def test_cagr_doubling_one_year():
    assert bs.cagr(pd.Series([100.0, 200.0]), 1.0) == pytest.approx(1.0, abs=1e-9)


def test_cagr_quadruple_two_years():
    # 4x over 2 years → 100%/yr compounded
    assert bs.cagr(pd.Series([100.0, 400.0]), 2.0) == pytest.approx(1.0, abs=1e-9)


def test_cagr_guards():
    assert np.isnan(bs.cagr(pd.Series([100.0, 200.0]), 0.0))   # non-positive years
    assert np.isnan(bs.cagr(pd.Series([100.0]), 1.0))          # < 2 points
    assert np.isnan(bs.cagr(pd.Series([0.0, 100.0]), 1.0))     # non-positive start


def test_max_drawdown_pct():
    # peak 120, trough 60 → 50% DD
    assert bs.max_drawdown_pct([100, 120, 60, 90]) == pytest.approx(0.5, abs=1e-9)
    assert bs.max_drawdown_pct([100, 110, 120]) == pytest.approx(0.0, abs=1e-9)  # monotonic up
    assert bs.max_drawdown_pct([100]) == 0.0                                     # < 2 points
