"""
Phase 4 verification: exit-signal logic, regime-flip exits, position routing.
Run from project root: python3 tests/test_phase4.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.filter_engine import FilterEngine, MarketRegime, SignalResult
from core.indicators.indicators import atr, macd, rsi


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_df(closes: np.ndarray) -> pd.DataFrame:
    """Build an enriched OHLCV DataFrame from a close-price array."""
    n  = len(closes)
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.01,
        "low":    closes * 0.99,
        "close":  closes,
        "volume": [1_000_000] * n,
    })
    df.index = pd.date_range("2025-01-01", periods=n, freq="B")
    df["atr"]         = atr(df)
    df["rsi"]         = rsi(df["close"])
    m, s, h           = macd(df["close"])
    df["macd"]        = m
    df["macd_signal"] = s
    df["macd_hist"]   = h
    return df


def _bull_uptrend_df() -> pd.DataFrame:
    return _make_df(np.linspace(50.0, 150.0, 250))


def _falling_df() -> pd.DataFrame:
    """A clean downtrend that turns up at the end — generates a fade pattern."""
    closes = np.concatenate([
        np.linspace(150.0, 80.0, 200),
        np.linspace(80.0, 82.0, 50),
    ])
    return _make_df(closes)


def _build_engine() -> FilterEngine:
    return FilterEngine(config_path=ROOT / "config" / "filters.yaml")


# ── test 1: regime flip triggers exit on held long ───────────────────────────

def test_regime_flip_exits_held_long():
    engine = _build_engine()
    df     = _bull_uptrend_df()

    # Build a synthetic BEAR market (indices below their own MA50)
    bear_close = np.linspace(100.0, 70.0, 250)
    bear_df    = _make_df(bear_close)

    result = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": bear_df, "QQQ": bear_df},
        vix_df     = None,
        held_long  = True,
    )
    assert result.passed,                    f"expected exit, got {result.reason}"
    assert result.direction   == "exit_long", f"direction = {result.direction}"
    assert result.signal_type == "regime_exit"
    assert "regime flipped" in result.reason.lower()
    print(f"  PASS  regime flip → exit_long ({result.reason})")


# ── test 2: unheld ticker in same conditions → no entry, no exit ─────────────

def test_unheld_ticker_no_exit_in_bear():
    engine = _build_engine()
    df     = _bull_uptrend_df()
    bear   = _make_df(np.linspace(100.0, 70.0, 250))

    result = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": bear, "QQQ": bear},
        vix_df     = None,
        held_long  = False,
    )
    # Entry mode: BEAR blocks longs, no signal fires.
    assert not result.passed
    assert result.direction == "none"
    print(f"  PASS  unheld in BEAR → no signal ({result.reason})")


# ── test 3: held long in BULL regime + no fade → hold ────────────────────────

def test_held_long_bull_no_fade_holds():
    engine = _build_engine()
    df     = _bull_uptrend_df()

    result = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": df, "QQQ": df},  # BULL
        vix_df     = None,
        held_long  = True,
    )
    assert not result.passed, "should hold (no exit condition met)"
    assert "no exit condition" in result.reason
    print(f"  PASS  held BULL + no fade → hold ({result.reason})")


# ── test 4: exits ignore stop_date blackouts ─────────────────────────────────

def test_exit_ignores_stop_dates():
    """
    Exit mode bypasses stop_date blackouts. Inject a synthetic stop_date
    matching 'today' and confirm a regime-flip exit still fires.
    """
    today = date(2026, 5, 14)
    engine = FilterEngine(
        config_path = ROOT / "config" / "filters.yaml",
        today       = today,
    )
    engine._cfg["events"]["stop_dates"] = [
        {"id": 99, "date": today.isoformat(), "description": "synthetic test blackout"}
    ]
    df   = _bull_uptrend_df()
    bear = _make_df(np.linspace(100.0, 70.0, 250))

    result = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": bear, "QQQ": bear},
        vix_df     = None,
        held_long  = True,
    )
    assert result.passed,                    f"expected exit, got {result.reason}"
    assert result.signal_type == "regime_exit"
    print(f"  PASS  exit bypasses stop_date blackout")


# ── test 5: entry mode IS blocked by stop_date (sanity) ──────────────────────

def test_entry_blocked_by_stop_date():
    """Force a stop_date for 'today' by injecting it into the engine's config,
    so the test is independent of whatever dates filters.yaml currently lists."""
    today = date(2026, 5, 14)
    engine = FilterEngine(
        config_path = ROOT / "config" / "filters.yaml",
        today       = today,
    )
    # Replace the stop_dates list with one matching 'today'
    engine._cfg["events"]["stop_dates"] = [
        {"id": 99, "date": today.isoformat(), "description": "synthetic test blackout"}
    ]
    df = _bull_uptrend_df()

    result = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": df, "QQQ": df},
        vix_df     = None,
        held_long  = False,
    )
    assert not result.passed
    assert "stop date" in result.reason.lower()
    print(f"  PASS  entry blocked by stop_date ({result.reason})")


# ── test 6: high volatility blocks entries but not exits ─────────────────────

def test_high_vol_blocks_entries_not_exits():
    engine = _build_engine()
    df     = _bull_uptrend_df()
    # VIX of 40 → HIGH (threshold vix_high=25 in filters.yaml)
    vix_df = _make_df(np.array([20.0] * 249 + [40.0]))

    # Entry: HIGH vol blocks
    entry = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": df, "QQQ": df},
        vix_df     = vix_df,
        held_long  = False,
    )
    assert not entry.passed
    assert "high vol" in entry.reason.lower() or "HIGH" in entry.market_regime

    # Exit on a held long with HIGH vol + regime flip → fires
    bear = _make_df(np.linspace(100.0, 70.0, 250))
    exit_sig = engine.signal(
        ticker     = "AAPL",
        df         = df,
        market_dfs = {"SPY": bear, "QQQ": bear},
        vix_df     = vix_df,
        held_long  = True,
    )
    assert exit_sig.passed
    assert exit_sig.direction == "exit_long"
    print(f"  PASS  HIGH vol blocks entry but not exit")


# ── test 7: SignalResult direction type covers new exit_long literal ─────────

def test_direction_literal_includes_exit_long():
    # Sanity check on the Literal definition
    import typing
    from core import filter_engine as fe
    # SignalResult should accept exit_long without complaint
    r = SignalResult(passed=True, direction="exit_long", signal_type="regime_exit")
    assert r.direction == "exit_long"
    print(f"  PASS  Direction literal includes exit_long")


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_regime_flip_exits_held_long,
        test_unheld_ticker_no_exit_in_bear,
        test_held_long_bull_no_fade_holds,
        test_exit_ignores_stop_dates,
        test_entry_blocked_by_stop_date,
        test_high_vol_blocks_entries_not_exits,
        test_direction_literal_includes_exit_long,
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
            print(f"  ERROR {type(e).__name__}: {e}")
    print(f"\n{'─' * 60}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(failures)
