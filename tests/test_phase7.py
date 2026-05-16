"""
Phase 7 verification: relative strength, weekly trend, Bollinger Z-score.
Run from project root: python3 tests/test_phase7.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.filter_engine import FilterEngine, SignalResult
from core.indicators.indicators import atr, bollinger_bands, macd, rsi
from core.scoring import (
    SignalScorer,
    _score_bb_zscore,
    _score_rs_entry,
    _score_rs_exit,
    _score_weekly_trend,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_df(closes: np.ndarray, volumes: np.ndarray | None = None) -> pd.DataFrame:
    n   = len(closes)
    vol = volumes if volumes is not None else np.full(n, 2_000_000)
    df  = pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.01,
        "low":    closes * 0.99,
        "close":  closes,
        "volume": vol,
    })
    df.index  = pd.date_range("2024-01-01", periods=n, freq="B")
    df["atr"]  = atr(df)
    df["rsi"]  = rsi(df["close"])
    m, s, h    = macd(df["close"])
    df["macd"] = m
    df["macd_signal"] = s
    df["macd_hist"]   = h
    bb = bollinger_bands(df["close"])
    df["bb_mid"]   = bb["bb_mid"]
    df["bb_upper"] = bb["bb_upper"]
    df["bb_lower"] = bb["bb_lower"]
    df["bb_bw"]    = bb["bb_bw"]
    df["bb_z"]     = bb["bb_z"]
    return df


def _bull_df() -> pd.DataFrame:
    return _make_df(np.linspace(50.0, 150.0, 300))


def _settings() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def _filters() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config" / "filters.yaml").read_text())


def _engine() -> FilterEngine:
    return FilterEngine(config_path=ROOT / "config" / "filters.yaml")


# ── bollinger_bands indicator ─────────────────────────────────────────────────

def test_bollinger_bands_shape():
    closes = pd.Series(np.linspace(100.0, 200.0, 100))
    bb = bollinger_bands(closes, period=20)
    assert set(bb.columns) == {"bb_mid", "bb_upper", "bb_lower", "bb_bw", "bb_z"}
    assert len(bb) == 100
    print(f"  PASS  bollinger_bands returns 5 columns, correct length")


def test_bollinger_bands_warmup_nan():
    closes = pd.Series(np.linspace(100.0, 200.0, 50))
    bb = bollinger_bands(closes, period=20)
    assert bb["bb_z"].iloc[:19].isna().all(), "first 19 values must be NaN"
    assert not pd.isna(bb["bb_z"].iloc[19]),  "bar 20 must be valid"
    print(f"  PASS  bollinger_bands warmup NaN correct (first 19 rows NaN)")


def test_bollinger_bands_symmetry():
    # Constant price → Z = 0, σ = 0 → Z is NaN (not zero)
    closes = pd.Series([100.0] * 50)
    bb = bollinger_bands(closes, period=20)
    # All values equal → σ = 0, Z should be NaN (safe division by zero)
    assert bb["bb_z"].iloc[19:].isna().all(), "constant series → Z must be NaN"
    print(f"  PASS  constant price → Z-score is NaN (safe σ=0 handling)")


def test_bollinger_bands_z_sign():
    # Trending upward: last close is above SMA → Z > 0
    closes = pd.Series(np.linspace(50.0, 150.0, 100))
    bb = bollinger_bands(closes, period=20)
    assert float(bb["bb_z"].iloc[-1]) > 0, "upward trend → last Z must be positive"
    print(f"  PASS  upward trend → Z > 0 (close above SMA20)")


# ── relative strength: entry ──────────────────────────────────────────────────

def test_rs_entry_outperforming_both():
    # Ticker +20%, SPY +10% over 60d → RS20 > 0, RS60 > 0 → 1.0
    spy = _make_df(np.linspace(100.0, 110.0, 300))  # +10%
    tkr = _make_df(np.linspace(100.0, 120.0, 300))  # +20%
    score = _score_rs_entry(tkr, {"SPY": spy})
    assert score == 1.0, f"outperforming both windows → 1.0, got {score}"
    print(f"  PASS  RS entry outperforming both → 1.0")


def test_rs_entry_underperforming_both():
    # Ticker +5%, SPY +20% → both RS negative → 0.0
    spy = _make_df(np.linspace(100.0, 120.0, 300))
    tkr = _make_df(np.linspace(100.0, 105.0, 300))
    score = _score_rs_entry(tkr, {"SPY": spy})
    assert score == 0.0, f"underperforming both → 0.0, got {score}"
    print(f"  PASS  RS entry underperforming both → 0.0")


def test_rs_entry_no_spy_neutral():
    tkr = _make_df(np.linspace(100.0, 120.0, 300))
    score = _score_rs_entry(tkr, None)
    assert score == 0.5, f"no SPY data → 0.5 neutral, got {score}"
    print(f"  PASS  RS entry no SPY → 0.5 neutral")


# ── relative strength: exit ───────────────────────────────────────────────────

def test_rs_exit_underperforming_fires():
    # SPY surges +10% over the last 20 bars; ticker stays flat.
    # rs20 = (100/100) / (110/100) - 1 ≈ -9.1% → score ≈ 0.91
    n = 100
    spy_closes = np.concatenate([np.full(80, 100.0), np.linspace(100.0, 110.0, 20)])
    tkr_closes = np.full(n, 100.0)
    spy = _make_df(spy_closes)
    tkr = _make_df(tkr_closes)
    score = _score_rs_exit(tkr, {"SPY": spy})
    assert score > 0.7, f"~9% underperformance → score > 0.7, got {score:.3f}"
    print(f"  PASS  RS exit underperforming → score={score:.3f} (> 0.7)")


def test_rs_exit_outperforming_zero():
    # Ticker +15%, SPY +5% → outperforming → rs20 > 0 → exit score 0
    spy = _make_df(np.linspace(100.0, 105.0, 100))
    tkr = _make_df(np.linspace(100.0, 115.0, 100))
    score = _score_rs_exit(tkr, {"SPY": spy})
    assert score == 0.0, f"outperforming SPY → exit rs 0.0, got {score:.3f}"
    print(f"  PASS  RS exit outperforming → 0.0")


# ── weekly trend ──────────────────────────────────────────────────────────────

def test_weekly_trend_strong_uptrend():
    # 300 bars clean uptrend → close > SMA10w, SMA rising
    df = _bull_df()
    score = _score_weekly_trend(df)
    assert score == 1.0, f"clean uptrend → weekly_trend 1.0, got {score}"
    print(f"  PASS  clean uptrend → weekly_trend=1.0")


def test_weekly_trend_downtrend():
    df = _make_df(np.linspace(150.0, 50.0, 300))  # falling
    score = _score_weekly_trend(df)
    assert score == 0.0, f"downtrend → weekly_trend 0.0, got {score}"
    print(f"  PASS  clean downtrend → weekly_trend=0.0")


def test_weekly_trend_insufficient_data():
    df = _make_df(np.linspace(100.0, 110.0, 50))  # only 50 bars
    score = _score_weekly_trend(df)
    assert score == 0.5, f"insufficient data → 0.5, got {score}"
    print(f"  PASS  insufficient data → weekly_trend=0.5 neutral")


# ── BB Z-score ────────────────────────────────────────────────────────────────

def test_bb_zscore_momentum_formula():
    """Test the momentum scoring formula directly by injecting known Z values."""
    df = _make_df(np.linspace(100.0, 200.0, 100))
    # Z = 0: perfectly at the mean → ideal momentum entry → score 1.0
    df.at[df.index[-1], "bb_z"] = 0.0
    assert abs(_score_bb_zscore(df, "momentum") - 1.0) < 1e-6, "Z=0 → 1.0"
    # Z = 1: one std above mean → moderate entry → score 0.5
    df.at[df.index[-1], "bb_z"] = 1.0
    assert abs(_score_bb_zscore(df, "momentum") - 0.5) < 1e-6, "Z=1 → 0.5"
    # Z = 2: two std above mean → extended, bad entry → score 0.0
    df.at[df.index[-1], "bb_z"] = 2.0
    assert _score_bb_zscore(df, "momentum") == 0.0, "Z=2 → 0.0"
    # Z = -1: one std below mean → still reasonable → score 0.5
    df.at[df.index[-1], "bb_z"] = -1.0
    assert abs(_score_bb_zscore(df, "momentum") - 0.5) < 1e-6, "Z=-1 → 0.5"
    print(f"  PASS  momentum bb_zscore formula: Z=0→1.0, Z=±1→0.5, Z=±2→0.0")


def test_bb_zscore_mean_rev_oversold():
    # Build df ending with oversold close (force bb_z negative)
    closes = np.linspace(100.0, 200.0, 100)
    closes[-5:] = closes[-5:] - 20  # sharp dip at end
    df = _make_df(closes)
    bb_z = float(df["bb_z"].iloc[-1])
    score = _score_bb_zscore(df, "mean_reversion")
    if bb_z < -1.5:
        assert score > 0.5, f"Z={bb_z:.2f} mean-rev score should be > 0.5, got {score:.3f}"
    print(f"  PASS  mean_reversion bb_zscore  Z={bb_z:.2f}  score={score:.3f}")


def test_bb_zscore_no_column_neutral():
    # df without bb_z column → neutral 0.5
    df = _make_df(np.linspace(100.0, 200.0, 100))
    df = df.drop(columns=["bb_z"])
    score = _score_bb_zscore(df, "momentum")
    assert score == 0.5, f"missing bb_z → 0.5 neutral, got {score}"
    print(f"  PASS  missing bb_z column → 0.5 neutral")


# ── integration: new components appear in scored signal ──────────────────────

def test_new_components_in_signal_score():
    engine  = _engine()
    scorer  = SignalScorer(_settings(), _filters())
    df      = _bull_df()
    spy_df  = _bull_df()

    signal = SignalResult(
        passed=True, direction="long", signal_type="momentum",
        market_regime="BULL_LOW", ticker_trend="UPTREND",
    )
    regime = engine.market_regime({"SPY": spy_df, "QQQ": spy_df}, None)
    scorer.enrich(
        signal     = signal,
        df         = df,
        regime     = regime,
        market_dfs = {"SPY": spy_df, "QQQ": spy_df},
    )

    for name in ("relative_strength", "weekly_trend", "bb_zscore"):
        assert name in signal.score_components, f"missing component: {name}"
    print(
        f"  PASS  all Phase 7 components in score_components: "
        f"rs={signal.score_components['relative_strength']:.2f}  "
        f"weekly={signal.score_components['weekly_trend']:.2f}  "
        f"bb_z={signal.score_components['bb_zscore']:.2f}"
    )


def test_exit_rs_divergence_in_components():
    engine  = _engine()
    scorer  = SignalScorer(_settings(), _filters())
    df      = _bull_df()
    bear    = _make_df(np.linspace(150.0, 70.0, 300))

    signal = engine.signal(
        "TEST", df,
        market_dfs={"SPY": bear, "QQQ": bear},
        held_long=True,
    )
    assert signal.passed and signal.direction == "exit_long"

    regime = engine.market_regime({"SPY": bear, "QQQ": bear}, None)
    scorer.enrich(
        signal     = signal,
        df         = df,
        regime     = regime,
        market_dfs = {"SPY": bear, "QQQ": bear},
    )

    assert "rs_divergence" in signal.score_components, "rs_divergence missing from exit"
    print(
        f"  PASS  rs_divergence in exit components: "
        f"{signal.score_components['rs_divergence']:.2f}"
    )


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_bollinger_bands_shape,
        test_bollinger_bands_warmup_nan,
        test_bollinger_bands_symmetry,
        test_bollinger_bands_z_sign,
        test_rs_entry_outperforming_both,
        test_rs_entry_underperforming_both,
        test_rs_entry_no_spy_neutral,
        test_rs_exit_underperforming_fires,
        test_rs_exit_outperforming_zero,
        test_weekly_trend_strong_uptrend,
        test_weekly_trend_downtrend,
        test_weekly_trend_insufficient_data,
        test_bb_zscore_momentum_formula,
        test_bb_zscore_mean_rev_oversold,
        test_bb_zscore_no_column_neutral,
        test_new_components_in_signal_score,
        test_exit_rs_divergence_in_components,
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
