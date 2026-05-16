"""
Phase 6 verification: confidence scoring, WATCH logic, current-price drift.
Run from project root: python3 tests/test_phase6.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.filter_engine import FilterEngine, SignalResult
from core.indicators.indicators import atr, macd, rsi
from core.scoring import SignalScorer, _weighted_average


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_df(closes: np.ndarray) -> pd.DataFrame:
    n  = len(closes)
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.01,
        "low":    closes * 0.99,
        "close":  closes,
        "volume": [2_000_000] * n,
    })
    df.index = pd.date_range("2025-01-01", periods=n, freq="B")
    df["atr"]         = atr(df)
    df["rsi"]         = rsi(df["close"])
    m, s, h           = macd(df["close"])
    df["macd"]        = m
    df["macd_signal"] = s
    df["macd_hist"]   = h
    return df


def _bull_df() -> pd.DataFrame:
    return _make_df(np.linspace(50.0, 150.0, 250))


def _settings() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def _filters() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "filters.yaml").read_text())


def _engine() -> FilterEngine:
    return FilterEngine(config_path=ROOT / "config" / "filters.yaml")


# ── test 1: _weighted_average normalises correctly ───────────────────────────

def test_weighted_average_normalises():
    components = {"a": 1.0, "b": 0.0}
    weights    = {"a": 25, "b": 25}
    score = _weighted_average(components, weights)
    assert abs(score - 50.0) < 1e-6, f"expected 50.0, got {score}"
    print(f"  PASS  weighted_average normalises  →  {score:.1f}")


def test_weighted_average_all_ones():
    components = {"trend_up": 1.0, "ma50_slope": 1.0, "volume_spike": 1.0,
                  "rsi_healthy": 1.0, "breakout_20d": 1.0,
                  "macd_bullish": 1.0, "no_earnings_risk": 1.0}
    weights = {"trend_up": 25, "ma50_slope": 15, "volume_spike": 20,
               "rsi_healthy": 15, "breakout_20d": 20,
               "macd_bullish": 15, "no_earnings_risk": 10}
    score = _weighted_average(components, weights)
    assert abs(score - 100.0) < 1e-6, f"all-one components should score 100, got {score}"
    print(f"  PASS  all-1.0 components → 100.0")


# ── test 2: entry signal gets scored ─────────────────────────────────────────

def test_entry_signal_gets_score():
    engine  = _engine()
    scorer  = SignalScorer(_settings(), _filters())
    df      = _bull_df()

    signal = engine.signal(
        "TEST", df,
        market_dfs = {"SPY": df, "QQQ": df},
        vix_df     = None,
        held_long  = False,
    )
    if not signal.passed:
        print(f"  SKIP  no entry trigger fired on this synthetic data ({signal.reason})")
        return

    regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
    scorer.enrich(signal=signal, df=df, regime=regime)

    assert signal.score > 0, "score must be > 0 after enrich"
    assert 0 <= signal.score <= 100, f"score {signal.score} out of range"
    assert signal.timeframe  == "daily"
    assert signal.expected_hold_days == (10, 15)
    assert signal.description, "description must not be empty"
    print(f"  PASS  entry signal scored {signal.score:.1f}/100")


# ── test 4: watch_only flag flips correctly ───────────────────────────────────

def test_watch_only_below_threshold():
    settings = _settings()
    engine   = _engine()
    scorer   = SignalScorer(settings, _filters())
    df       = _bull_df()

    # Force a score below min_score_to_alert by passing all-zero components
    signal = SignalResult(passed=True, direction="long", signal_type="momentum",
                         market_regime="BULL_LOW", ticker_trend="UPTREND")

    # Monkeypatch _score_entry to return 0
    from core import scoring as sc_mod
    original = sc_mod._score_entry
    sc_mod._score_entry = lambda *a, **kw: (0.0, {k: 0.0 for k in settings["scanner"]["weights"]})
    try:
        regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
        scorer.enrich(signal=signal, df=df, regime=regime)
    finally:
        sc_mod._score_entry = original

    assert signal.watch_only is True, "score=0 should always be watch_only"
    print(f"  PASS  score=0 → watch_only=True")


def test_not_watch_only_above_threshold():
    settings = _settings()
    engine   = _engine()
    scorer   = SignalScorer(settings, _filters())
    df       = _bull_df()

    signal = SignalResult(passed=True, direction="long", signal_type="momentum",
                         market_regime="BULL_LOW", ticker_trend="UPTREND")

    from core import scoring as sc_mod
    original = sc_mod._score_entry
    sc_mod._score_entry = lambda *a, **kw: (95.0, {k: 1.0 for k in settings["scanner"]["weights"]})
    try:
        regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
        scorer.enrich(signal=signal, df=df, regime=regime)
    finally:
        sc_mod._score_entry = original

    assert signal.watch_only is False, f"score=95 should not be watch_only"
    print(f"  PASS  score=95 → watch_only=False")


# ── test 5: exit signal gets scored ──────────────────────────────────────────

def test_exit_signal_gets_score():
    engine = _engine()
    scorer = SignalScorer(_settings(), _filters())
    df     = _bull_df()
    bear   = _make_df(np.linspace(100.0, 70.0, 250))

    signal = engine.signal(
        "TEST", df,
        market_dfs = {"SPY": bear, "QQQ": bear},
        held_long  = True,
    )
    assert signal.passed
    assert signal.direction == "exit_long"

    regime = engine.market_regime({"SPY": bear, "QQQ": bear}, None)
    scorer.enrich(signal=signal, df=df, regime=regime)

    assert signal.score > 0
    assert "regime_flip" in signal.score_components
    assert signal.score_components["regime_flip"] == 1.0
    print(f"  PASS  exit signal scored {signal.score:.1f}/100  regime_flip=1.0")


# ── test 6: description contains expected fields ─────────────────────────────

def test_description_contains_key_fields():
    engine = _engine()
    scorer = SignalScorer(_settings(), _filters())
    df     = _bull_df()

    signal = SignalResult(passed=True, direction="long", signal_type="momentum",
                         stop_price=90.0, target_price=120.0, min_rr=2.0,
                         market_regime="BULL_LOW", ticker_trend="UPTREND")

    regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
    scorer.enrich(signal=signal, df=df, regime=regime)

    desc = signal.description
    assert "close=" in desc,      "description missing close="
    assert "RSI="   in desc,      "description missing RSI="
    assert "MACD"   in desc,      "description missing MACD"
    assert "ATR="   in desc,      "description missing ATR="
    assert "vol×"   in desc,      "description missing vol×"
    assert "regime" in desc,      "description missing regime"
    assert "components" in desc,  "description missing components"
    print(f"  PASS  description contains close/RSI/MACD/ATR/vol/regime/components")


# ── test 8: current price drift in description ───────────────────────────────

def test_description_shows_current_price_drift():
    engine = _engine()
    scorer = SignalScorer(_settings(), _filters())
    df     = _bull_df()

    signal = SignalResult(passed=True, direction="long", signal_type="momentum",
                         stop_price=90.0, target_price=120.0, min_rr=2.0,
                         market_regime="BULL_LOW", ticker_trend="UPTREND")

    regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
    signal_close = float(df["close"].iloc[-1])
    live         = signal_close * 1.02
    scorer.enrich(signal=signal, df=df, regime=regime, current_price=live)

    assert "current price=" in signal.description, "description missing current price"
    assert "drift" in signal.description,          "description missing drift label"
    print(f"  PASS  current price drift appears in description  (live={live:.2f})")


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = _engine()
    scorer = SignalScorer(_settings(), _filters())
    df     = _bull_df()

    signal = SignalResult(passed=True, direction="long", signal_type="momentum",
                         stop_price=90.0, target_price=120.0, min_rr=2.0,
                         market_regime="BULL_LOW", ticker_trend="UPTREND")

    regime = engine.market_regime({"SPY": df, "QQQ": df}, None)
    # Provide a synthetic current_price 2% above signal bar close
    signal_close = float(df["close"].iloc[-1])
    live          = signal_close * 1.02
    scorer.enrich(signal=signal, df=df, regime=regime, current_price=live)

    assert "current price=" in signal.description, "description missing current price"
    assert "drift" in signal.description,          "description missing drift label"
    assert "+2." in signal.description or "2.0" in signal.description
    print(f"  PASS  current price drift appears in description  (live={live:.2f})")


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_weighted_average_normalises,
        test_weighted_average_all_ones,
        test_entry_signal_gets_score,
        test_watch_only_below_threshold,
        test_not_watch_only_above_threshold,
        test_exit_signal_gets_score,
        test_description_contains_key_fields,
        test_description_shows_current_price_drift,
    ]
    failures = 0
    for t in tests:
        print(f"\n→ {t.__name__}")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {e}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"  ERROR {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'─' * 60}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(failures)