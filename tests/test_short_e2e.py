"""
Smoke test — short path end-to-end through the public
``FilterEngine.signal()`` API on synthetic BEAR data.

Distinct from ``test_short_signals.py``: those tests call the private
trigger methods directly or monkeypatch them to True. Here the *real*
``_momentum_short_entry`` must fire from a hand-built MACD-hist
down-cross, driven purely by the ``signals.allow_shorts`` config switch
— this is the smoke test for the 10.5 CLI plumbing (``--allow-shorts``
sets exactly this config key).

The market regime is still stubbed to BEAR/LOW because regime
classification needs index data we don't have in-sandbox; everything
downstream of that (trigger evaluation, short SignalResult
construction, stop/target geometry) runs for real.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from core.filter_engine import FilterEngine, MarketRegime


# ─── helpers ─────────────────────────────────────────────────────────────────


def _engine(*, allow_shorts: bool) -> FilterEngine:
    """Real engine from production filters.yaml with shorts toggled."""
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "filters.yaml")
        .read_text(encoding="utf-8")
    )
    cfg["signals"]["allow_shorts"] = allow_shorts
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)
    # Regime + ticker-trend need index data; stub to a short-friendly state.
    eng._market_regime = lambda md, vd: MarketRegime(trend="BEAR", volatility="LOW")
    eng._ticker_trend = lambda d: "DOWNTREND"
    return eng


def _bear_df(n: int = 220) -> pd.DataFrame:
    """220-bar frame whose MACD histogram crosses DOWN one bar ago.

    Tuned so the *real* ``_momentum_short_entry`` fires: RSI in the
    [30, 50] short band, histogram negative with a negative delta, and
    a zero-cross within ``max_bars_since_cross`` (3).
    """
    # macd_hist: long positive run, then 0.1 (prev) → -0.2 (last) = cross 1 bar back.
    macd_hist = [0.5] * (n - 2) + [0.1, -0.2]
    base = {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1_000_000.0,
        "atr": 1.0,
        "rsi": 45.0,  # inside the short_entry band
        "macd": 0.0,
        "macd_signal": 0.0,
        "ma_fast": 95.0,
        "ma_slow": 90.0,
    }
    df = pd.DataFrame([dict(base) for _ in range(n)],
                      index=pd.date_range("2024-01-01", periods=n, freq="B"))
    df["macd_hist"] = macd_hist
    return df


# ─── tests ───────────────────────────────────────────────────────────────────


def test_allow_shorts_config_produces_short_via_public_signal():
    """allow_shorts=True + BEAR regime → real trigger fires a short through
    the public signal() API, with short-correct stop/target geometry."""
    eng = _engine(allow_shorts=True)
    df = _bear_df()

    result = eng.signal("BEAR", df, market_dfs=None, vix_df=None, earnings_date=None)

    assert result.passed is True
    assert result.direction == "short"
    assert result.signal_type == "momentum"

    last_close = float(df["close"].iloc[-1])
    assert result.stop_price > last_close, "short stop must sit above entry"
    assert result.target_price < last_close, "short target must sit below entry"


def test_allow_shorts_false_blocks_short_end_to_end():
    """Same BEAR data, flag off → no short emitted. Guards the baseline:
    the long-only replay is unaffected unless --allow-shorts is passed."""
    eng = _engine(allow_shorts=False)
    df = _bear_df()

    result = eng.signal("BEAR", df, market_dfs=None, vix_df=None, earnings_date=None)

    assert result.direction != "short"
    assert not (result.passed and result.direction == "short")
