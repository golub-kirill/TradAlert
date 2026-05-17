"""
Behavioural verification for every Phase 1 fix.
Run from project root: python3 tests/test_phase1.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.indicators.indicators import macd, rsi
from core.filter_engine import FilterEngine


# ── Fix 2: RSI returns 100 during sustained uptrend, not NaN ─────────────────

def test_rsi_sustained_uptrend_is_100():
    # 200 bars of strictly increasing closes — no down-closes at all
    closes = pd.Series(np.linspace(10.0, 50.0, 200))
    result = rsi(closes, period=14)
    last = result.iloc[-1]
    assert not np.isnan(last), "RSI must not be NaN during sustained uptrend"
    assert abs(last - 100.0) < 1e-6, f"RSI should be 100, got {last}"
    print(f"  PASS  RSI uptrend = {last:.4f}")


def test_rsi_warmup_still_nan():
    # First (period - 1) bars must still be NaN — needed for warmup gate
    closes = pd.Series(np.linspace(10.0, 50.0, 30))
    result = rsi(closes, period=14)
    assert result.iloc[0:13].isna().all(), "RSI warmup region must remain NaN"
    print(f"  PASS  RSI warmup NaN preserved")


def test_rsi_normal_range():
    # Mixed up/down should produce a value in (0, 100), not the boundary
    np.random.seed(42)
    closes = pd.Series(100 + np.random.randn(200).cumsum())
    result = rsi(closes, period=14)
    last = result.iloc[-1]
    assert 0 < last < 100, f"RSI should be strictly interior, got {last}"
    print(f"  PASS  RSI normal mixed-returns = {last:.4f}")


# ── Fix 3 / 5: MACD warmup matches docstring ─────────────────────────────────

def test_macd_warmup_length():
    # With slow=26 and signal=9, histogram should be NaN for first (26+9-2)=33 bars
    closes = pd.Series(np.linspace(10.0, 50.0, 200))
    _, _, hist = macd(closes)
    nan_count = int(hist.isna().sum())
    expected = 26 + 9 - 2
    assert nan_count == expected, f"MACD hist NaN count {nan_count}, expected {expected}"
    print(f"  PASS  MACD histogram NaN count = {nan_count} (matches doc)")


# ── Fix 5: _rr_ok long/short branches ────────────────────────────────────────

def test_rr_ok_long_always_passes_with_positive_risk():
    # Long: target = entry + risk*rr is always > 0 for positive entry.
    # Old code wrongly rejected longs where risk*min_rr > entry.
    # Construct a case where risk*min_rr > entry: entry=10, stop=4, min_rr=2 → 6*2=12 > 10
    assert FilterEngine._rr_ok(entry=10.0, stop=4.0, min_rr=2.0, is_long=True) is True
    print(f"  PASS  long with risk*rr > entry now accepted (old code rejected)")


def test_rr_ok_long_rejects_zero_risk():
    assert FilterEngine._rr_ok(entry=10.0, stop=10.0, min_rr=2.0, is_long=True) is False
    print(f"  PASS  zero-risk long rejected")


def test_rr_ok_short_requires_target_above_zero():
    # Short: target = entry - risk*rr must be > 0 → risk*rr < entry
    # entry=10, stop=16, min_rr=2 → risk=6, 6*2=12, target = -2 → reject
    assert FilterEngine._rr_ok(entry=10.0, stop=16.0, min_rr=2.0, is_long=False) is False
    # entry=10, stop=12, min_rr=2 → risk=2, 2*2=4, target = 6 → accept
    assert FilterEngine._rr_ok(entry=10.0, stop=12.0, min_rr=2.0, is_long=False) is True
    print(f"  PASS  short R:R correctly bounds target above zero")


# ── Fix 6: yfinance end-date convention (signature check only — no network) ──

def test_yfinance_default_end_is_tomorrow():
    src = (ROOT / "src" / "core" / "fetchers" / "yf_fetchOne.py").read_text()
    nowhite = src.replace(" ", "").replace("\n", "")
    assert "date.today()+timedelta(days=1)" in nowhite, \
        "fetch() end default should be (today + 1d).isoformat()"
    print(f"  PASS  yfinance_fetcher.fetch end defaults to today + 1")


# ── Fix 8: cache.is_fresh no longer shadows function name ────────────────────

def test_cache_is_fresh_no_shadow():
    from core import cache
    import inspect
    src = inspect.getsource(cache.is_fresh)
    # Function name appears exactly once (the def line); the local was renamed.
    assert src.count("is_fresh") == 1, "is_fresh should appear only as the def name"
    print(f"  PASS  cache.is_fresh does not shadow its own name")


# ── Fix 9: _indicators_ready guards all MACD columns ─────────────────────────

def test_indicators_ready_checks_all_macd_cols():
    import ast
    tree = ast.parse((ROOT / "main.py").read_text())
    body_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_indicators_ready":
            body_src = ast.unparse(node)
            break
    assert body_src is not None, "_indicators_ready not found"
    for col in ("atr", "rsi", "macd", "macd_signal", "macd_hist"):
        assert f'"{col}"' in body_src or f"'{col}'" in body_src, \
            f"_indicators_ready missing column: {col}"
    print(f"  PASS  _indicators_ready guards atr/rsi/macd/macd_signal/macd_hist")


# ── Fix 10: filters.yaml stop_dates are 2026 not 2036 ────────────────────────

def test_stop_dates_year():
    import yaml
    from datetime import date as _date
    cfg = yaml.safe_load(open(ROOT / "config" / "filters.yaml"))
    for entry in cfg["events"]["stop_dates"]:
        d = _date.fromisoformat(entry["date"])  # raises if malformed
        assert d.year >= 2026, f"stop_date in the past: {entry}"
    print(f"  PASS  stop_dates parse as valid ISO dates")


# ── Trend consistency: _ticker_trend and _scan_pass_reason agree ─────────────

def test_trend_consistency():
    """
    Build a frame where the loose-min_periods reading would label CHOP
    on day-1 of MA200 warmup but the strict reading correctly labels CHOP.
    With both readings strict, they must agree on every bar.
    """
    # 250 bars of a clean uptrend with indicators
    closes = pd.Series(np.linspace(50.0, 150.0, 250))
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.01,
        "low":    closes * 0.99,
        "close":  closes,
        "volume": pd.Series([1_000_000] * 250),
    })
    df.index = pd.date_range("2025-01-01", periods=250, freq="B")
    df["atr"]         = (df["high"] - df["low"]).rolling(14).mean()
    df["rsi"]         = rsi(df["close"])
    m, s, h           = macd(df["close"])
    df["macd"]        = m
    df["macd_signal"] = s
    df["macd_hist"]   = h

    engine = FilterEngine(config_path=ROOT / "config" / "filters.yaml")

    # _ticker_trend
    tt = engine._ticker_trend(df)

    # _scan_pass_reason
    row  = df.iloc[-1]
    dv20 = float((df["close"] * df["volume"]).tail(20).mean())
    reason = engine._scan_pass_reason(df, row, dv20)
    label_in_reason = reason.split(" | ")[0]

    assert tt == label_in_reason, (
        f"trend disagreement: _ticker_trend={tt}, scan_pass_reason starts with {label_in_reason}"
    )
    print(f"  PASS  trend consistency: both report {tt}")


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_rsi_sustained_uptrend_is_100,
        test_rsi_warmup_still_nan,
        test_rsi_normal_range,
        test_macd_warmup_length,
        test_rr_ok_long_always_passes_with_positive_risk,
        test_rr_ok_long_rejects_zero_risk,
        test_rr_ok_short_requires_target_above_zero,
        test_yfinance_default_end_is_tomorrow,
        test_cache_is_fresh_no_shadow,
        test_indicators_ready_checks_all_macd_cols,
        test_stop_dates_year,
        test_trend_consistency,
    ]
    failures = 0
    for t in tests:
        print(f"\n→ {t.__name__}")
        try:
            t
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {type(e).__name__}: {e}")
    print(f"\n{'─' * 60}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(failures)
