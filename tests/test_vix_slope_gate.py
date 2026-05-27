"""
Tests for the VIX slope gate.

Covers two surfaces:
  1. ``MarketRegime.vix_rising`` populated by ``FilterEngine._market_regime``
     from the configured lookback window.
  2. ``FilterEngine._evaluate_entry`` blocking momentum entries when
     ``regime.vix_slope_block`` is enabled and ``vix_rising`` is True.

Mean-reversion entries are intentionally not gated; verified explicitly.

Run with::

    pytest tests/test_vix_slope_gate.py -v
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd
import pytest

from core.filter_engine import FilterEngine, MarketRegime


# ─── helpers ─────────────────────────────────────────────────────────────────


def _vix_df(values: Sequence[float]) -> pd.DataFrame:
    """Synthetic VIX OHLCV DataFrame with daily index."""
    idx = pd.date_range("2025-01-01", periods=len(values), freq="B")
    return pd.DataFrame(
        {
            "open": values, "high": values, "low": values,
            "close": list(values), "volume": [0] * len(values),
        },
        index=idx,
    )


def _spy_qqq(values: Sequence[float]) -> dict[str, pd.DataFrame]:
    """SPY and QQQ both stable at the given close so trend = BULL when above MA."""
    idx = pd.date_range("2024-01-01", periods=len(values), freq="B")
    df = pd.DataFrame({"close": list(values)}, index=idx)
    return {"SPY": df, "QQQ": df}


def _engine(cfg_overrides: dict | None = None) -> FilterEngine:
    """Build a FilterEngine from the real ``config/filters.yaml`` plus optional overrides.

    The engine validates a long list of required keys at construction
    time (``_REQUIRED_CONFIG_KEYS``); rather than re-listing every gate
    here we anchor on the production config and shallow-merge overrides
    onto the dicts that the tests need to mutate (e.g. ``regime``).
    """
    import yaml
    from pathlib import Path as _Path
    cfg_path = _Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    return FilterEngine.from_dict(cfg)


# ─── vix_rising classifier behaviour ─────────────────────────────────────────


def test_vix_rising_true_when_today_above_lookback():
    """VIX climbs from 16 → 19 over 5 bars → rising, still in LOW band."""
    eng = _engine()
    # close[-1]=19, close[-6]=16, so vix_rising=True. 19 < vix_low=20 → LOW.
    vix = _vix_df([16, 16.5, 17, 18, 18.5, 19])
    regime = eng.market_regime(_spy_qqq([100] * 30), vix)
    assert regime.vix_rising is True
    # This is the Feb 2025 shape exactly: LOW + rising → gate kicks in.
    assert regime.volatility == "LOW"


def test_vix_rising_false_when_today_below_lookback():
    """VIX falls from 18 → 15 → not rising."""
    eng = _engine()
    vix = _vix_df([18, 17.5, 17, 16, 15.5, 15])
    regime = eng.market_regime(_spy_qqq([100] * 30), vix)
    assert regime.vix_rising is False


def test_vix_rising_false_when_flat():
    """Equal endpoints → not rising (strict >)."""
    eng = _engine()
    vix = _vix_df([16, 17, 18, 17, 16, 16])  # close[-1] == close[-6]
    regime = eng.market_regime(_spy_qqq([100] * 30), vix)
    assert regime.vix_rising is False


def test_vix_rising_false_when_history_too_short():
    """Fewer bars than lookback → defaults to False (defensive)."""
    eng = _engine()
    vix = _vix_df([16, 17])  # only 2 bars; lookback default is 5
    regime = eng.market_regime(_spy_qqq([100] * 30), vix)
    assert regime.vix_rising is False


def test_vix_rising_defaults_false_when_no_vix_df():
    eng = _engine()
    regime = eng.market_regime(_spy_qqq([100] * 30), None)
    assert regime.vix_rising is False


def test_vix_slope_custom_lookback():
    """Lookback knob is honoured."""
    eng = _engine({"regime": {"vix_slope_lookback_days": 2}})
    vix = _vix_df([20, 21, 22, 21, 22])  # last vs 2-bar-ago: 22 vs 22 = not rising
    regime = eng.market_regime(_spy_qqq([100] * 30), vix)
    assert regime.vix_rising is False

    # 1-bar lookback: 22 vs prior 21 → rising
    eng2 = _engine({"regime": {"vix_slope_lookback_days": 1}})
    regime2 = eng2.market_regime(_spy_qqq([100] * 30), vix)
    assert regime2.vix_rising is True


# ─── entry-gate behaviour ────────────────────────────────────────────────────


def test_evaluate_entry_blocks_momentum_when_gate_on_and_rising():
    eng = _engine({"regime": {"vix_slope_block": True}})
    regime = MarketRegime(trend="BULL", volatility="LOW", vix_rising=True)

    # Construct a row/prev that would otherwise trigger momentum long.
    # _momentum_long checks rsi/macd_hist/bars-since-cross — we side-step
    # by monkey-patching it to always return True for this unit test.
    eng._momentum_long = lambda *a, **kw: True
    eng._mean_rev_long = lambda *a, **kw: False

    row = pd.Series({"atr": 1.0, "close": 100.0})
    prev = pd.Series({"atr": 1.0, "close": 99.0})
    df = pd.DataFrame([prev, row])

    direction, sigtype, reason = eng._evaluate_entry(
        row, prev, df, regime, "UPTREND",
    )
    assert direction == "none"
    assert "VIX slope-up" in reason


def test_evaluate_entry_allows_momentum_when_gate_off():
    eng = _engine({"regime": {"vix_slope_block": False}})
    regime = MarketRegime(trend="BULL", volatility="LOW", vix_rising=True)

    eng._momentum_long = lambda *a, **kw: True
    eng._mean_rev_long = lambda *a, **kw: False

    row = pd.Series({"atr": 1.0, "close": 100.0})
    prev = pd.Series({"atr": 1.0, "close": 99.0})
    df = pd.DataFrame([prev, row])

    direction, sigtype, _ = eng._evaluate_entry(
        row, prev, df, regime, "UPTREND",
    )
    assert direction == "long"
    assert sigtype == "momentum"


def test_evaluate_entry_allows_momentum_when_gate_on_but_vix_not_rising():
    eng = _engine({"regime": {"vix_slope_block": True}})
    regime = MarketRegime(trend="BULL", volatility="LOW", vix_rising=False)

    eng._momentum_long = lambda *a, **kw: True
    eng._mean_rev_long = lambda *a, **kw: False

    row = pd.Series({"atr": 1.0, "close": 100.0})
    prev = pd.Series({"atr": 1.0, "close": 99.0})
    df = pd.DataFrame([prev, row])

    direction, sigtype, _ = eng._evaluate_entry(
        row, prev, df, regime, "UPTREND",
    )
    assert direction == "long"
    assert sigtype == "momentum"


def test_evaluate_entry_mean_reversion_unaffected_by_slope_gate():
    """Mean-reversion entries are intentionally not gated by VIX slope."""
    eng = _engine({"regime": {"vix_slope_block": True}})
    regime = MarketRegime(trend="BULL", volatility="LOW", vix_rising=True)

    # momentum doesn't fire; mean-rev does → should be allowed.
    eng._momentum_long = lambda *a, **kw: False
    eng._mean_rev_long = lambda *a, **kw: True

    row = pd.Series({"atr": 1.0, "close": 100.0})
    prev = pd.Series({"atr": 1.0, "close": 99.0})
    df = pd.DataFrame([prev, row])

    direction, sigtype, _ = eng._evaluate_entry(
        row, prev, df, regime, "UPTREND",
    )
    assert direction == "long"
    assert sigtype == "mean_reversion"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
