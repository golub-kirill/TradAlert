"""
Regression tests for audit-driven changes (2026-05-31):
  - signals.size_mult_gate blocks/allows entries by composite size multiplier.
  - Reporting (equity curve + stats) aggregates Trade.effective_r, so the
    macro/behavioral position-size multiplier and borrow drag reach the
    headline numbers.
"""
from __future__ import annotations

import types
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from backtest.equity_curve import build_curve
from backtest.stats import compute_stats
from backtest.trade import Trade
from core.filter_engine import FilterEngine, MarketRegime
from core.indicators.indicators import attach_indicators


def _engine(overrides: dict | None = None) -> FilterEngine:
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "filters.yaml").read_text("utf-8")
    )
    for k, v in (overrides or {}).items():
        cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return FilterEngine.from_dict(cfg)


def _firing_df(n: int = 260) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(50.0, 100.0, n), index=idx)
    df = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": 1_000_000.0},
        index=idx,
    )
    return attach_indicators(df)


def _low_size_regime(mult: float) -> MarketRegime:
    macro = types.SimpleNamespace(size_multiplier=mult)
    return MarketRegime(trend="BULL", volatility="NORMAL", macro=macro)


# ── size_mult_gate ────────────────────────────────────────────────────────────

_SIG_OVERRIDE = {"signals": {"gap_risk": {"enabled": False},
                             "sector_gate": {"enabled": False}}}


def test_size_mult_gate_blocks_low_multiplier():
    ov = {"signals": {**_SIG_OVERRIDE["signals"],
                      "size_mult_gate": {"enabled": True, "min": 0.25}}}
    eng = _engine(ov)
    eng._evaluate_entry = lambda *a, **k: ("long", "momentum", "test-fire")
    regime = _low_size_regime(0.05)  # composite = sqrt(0.05) ≈ 0.224 < 0.25
    sig = eng._signal_entry("X", _firing_df(), regime, None, None)
    assert sig.passed is False
    assert "size_mult" in sig.reason


def test_size_mult_gate_off_allows_low_multiplier():
    ov = {"signals": {**_SIG_OVERRIDE["signals"],
                      "size_mult_gate": {"enabled": False, "min": 0.25}}}
    eng = _engine(ov)
    eng._evaluate_entry = lambda *a, **k: ("long", "momentum", "test-fire")
    sig = eng._signal_entry("X", _firing_df(), _low_size_regime(0.05), None, None)
    assert sig.passed is True
    assert sig.direction == "long"


# ── effective_r in reporting ──────────────────────────────────────────────────

def _trade(r_multiple: float, size_mult: float = 1.0) -> Trade:
    t = Trade(
        ticker="X", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=100.0,
        initial_stop=98.0, initial_target=104.0,
        exit_date=date(2024, 1, 8), exit_price=104.0, exit_reason="target",
        bars_held=5, size_mult=size_mult,
    )
    t.r_multiple = r_multiple
    return t


def test_effective_r_scales_reported_totals():
    # r=+2 at half size → +1.0 ; r=-1 at full size → -1.0 ; total = 0.0
    trades = [_trade(2.0, size_mult=0.5), _trade(-1.0, size_mult=1.0)]
    assert build_curve(trades).total_r == pytest.approx(0.0)
    assert compute_stats(trades).total_r == pytest.approx(0.0)


def test_effective_r_equals_r_multiple_at_full_size():
    trades = [_trade(2.0, 1.0), _trade(-1.0, 1.0)]
    assert compute_stats(trades).total_r == pytest.approx(1.0)


# ── MA-column fast path is result-identical to the rolling recompute ──────────

def _trend_via_rolling(close: pd.Series, fast: int, slow: int) -> str:
    if len(close) < slow:
        return "CHOP"
    mf = close.rolling(fast, min_periods=fast).mean().iloc[-1]
    ms = close.rolling(slow, min_periods=slow).mean().iloc[-1]
    last = close.iloc[-1]
    if last > mf > ms:
        return "UPTREND"
    if last < mf < ms:
        return "DOWNTREND"
    return "CHOP"


def test_ticker_trend_column_path_matches_rolling_recompute():
    eng = _engine()
    rng = np.random.default_rng(7)
    for seed in range(4):
        n = 380
        steps = rng.normal(0.1, 1.0, n).cumsum()
        close = pd.Series(50 + steps - steps.min() + 1.0,
                          index=pd.date_range("2022-01-01", periods=n, freq="B"))
        df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                           "close": close, "volume": 1e6}, index=close.index)
        df = attach_indicators(df)
        for T in range(205, n, 11):
            sl = df.iloc[:T + 1]
            assert eng._ticker_trend(sl) == _trend_via_rolling(sl["close"], 50, 200)


# ── scan() min-rows unification + NaN guard ───────────────────────────────────

def test_scan_requires_ma_slow_rows():
    import pytest as _pytest
    from exceptions import InsufficientDataError
    eng = _engine()
    ma_slow = 200
    with _pytest.raises(InsufficientDataError):
        eng.scan("X", _firing_df(ma_slow - 50))  # 150 < 200


def test_scan_blocks_nan_indicators_instead_of_passing():
    eng = _engine()
    df = _firing_df(260)
    # Corrupt the last bar's ATR to NaN (warmup-like). Old behaviour: NaN
    # comparisons silently pass the volatility gate. New: blocked.
    df = df.copy()
    df.loc[df.index[-1], "atr"] = np.nan
    res = eng.scan("X", df)
    assert res.passed is False
    assert "warmup" in res.reason.lower()


# ── COT positioning: lev_net (TFF) consumer + fail-open on schema mismatch ─────

def test_classify_positioning_uses_lev_net_and_fails_open():
    from core.behavioral import _classify_positioning
    n = 300
    idx = pd.date_range("2019-01-01", periods=n, freq="W-FRI")
    naaim = pd.DataFrame({"exposure": np.linspace(10, 90, n)}, index=idx)
    valid = ("CROWDED_LONG", "CROWDED_SHORT", "NEUTRAL")

    cot = pd.DataFrame({"lev_net": np.linspace(-1000, 5000, n)}, index=idx)
    assert _classify_positioning(cot, naaim) in valid          # lev_net consumed

    # Non-empty COT frame that lacks lev_net (e.g. old/Disaggregated schema)
    # must NOT raise KeyError — the axis degrades, NAAIM still counts.
    bad_cot = pd.DataFrame({"mm_net": np.linspace(-1000, 5000, n)}, index=idx)
    assert _classify_positioning(bad_cot, naaim) in valid
