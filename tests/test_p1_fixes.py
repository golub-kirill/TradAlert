"""Regression tests for the P1 correctness fixes (2026-06-07 audit batch).

Each test pins one bug the audit found and the fix closed:
  * behavioral ``missing_axes`` key mismatch + negative confidence
  * profit-factor ``r == 0`` convention split (stats vs stats_utils)
  * negative-caching of failed live-price / market-cap fetches
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.stats import compute_stats
from backtest.stats_utils import _profit_factor
from core.behavioral import classify_behavioral_state
from core.fetchers import info_fetcher, live_price


# ── behavioral missing-axes key mismatch (#2) ─────────────────────────────────

def test_behavioral_confidence_reflects_present_axis_count():
    """One of three axes present → confidence 1/3 (sentiment axis purged).
    Before the original fix the short-name mismatch drove this to 0.0."""
    idx = pd.date_range("2020-01-01", periods=30, freq="D")
    breadth = pd.DataFrame({"pct_above_ma200": [80.0] * 30}, index=idx)
    spy = pd.DataFrame({"open": 95.0, "high": 100.0, "low": 90.0, "close": 95.0}, index=idx)

    st = classify_behavioral_state({"breadth": breadth}, settings={}, spy_df=spy, as_of=None)

    assert st.confidence == 0.3333
    assert set(st.missing_axes) == {"sector_cycle", "positioning_state"}


def test_behavioral_all_missing_confidence_non_negative():
    """All axes missing → confidence 0.0, not the old −0.25 (positioning counted twice)."""
    st = classify_behavioral_state({}, settings={}, spy_df=None, as_of=None)
    assert st.confidence == 0.0
    assert set(st.missing_axes) == {
        "breadth_state", "sector_cycle", "positioning_state"}


def test_behavioral_partial_positioning_not_marked_missing():
    """COT present, NAAIM absent → positioning is NOT missing (marked only when both gone)."""
    idx = pd.date_range("2020-01-01", periods=60, freq="W-FRI")
    cot = pd.DataFrame({"net_noncommercial": np.linspace(-100, 100, 60)}, index=idx)
    st = classify_behavioral_state({"cot_es": cot}, settings={}, spy_df=None, as_of=None)
    assert "positioning_state" not in st.missing_axes


# ── profit-factor r==0 convention (#6) ────────────────────────────────────────

def _trade(r: float, bars: int = 5):
    return SimpleNamespace(is_closed=True, effective_r=float(r), bars_held=bars)


def test_scratch_is_neither_win_nor_loss():
    s = compute_stats([_trade(1.0), _trade(1.0), _trade(0.0)])
    assert s.wins == 2
    assert s.losses == 0                         # the 0.0 scratch is not a loss
    assert s.profit_factor == float("inf")        # no real losses → inf (was a crash)


def test_profit_factor_agrees_with_stats_utils_when_scratch_present():
    rs = [2.0, -1.0, 0.0, 1.5, -0.5]
    s = compute_stats([_trade(r) for r in rs])
    assert s.losses == 2                          # only r < 0 (scratch excluded)
    assert s.profit_factor == pytest.approx(_profit_factor(np.array(rs)))


# ── walk-forward IS selection trade-count floor (#3) ──────────────────────────

def test_wf_select_best_is_applies_trade_floor():
    from backtest.walk_forward import WalkForwardEngine
    pts = [
        SimpleNamespace(stats=SimpleNamespace(expectancy_r=9.0, trades_count=2)),    # fluke
        SimpleNamespace(stats=SimpleNamespace(expectancy_r=0.30, trades_count=50)),  # robust
    ]
    # Floor rejects the 2-trade fluke → the robust combo wins.
    assert WalkForwardEngine._select_best_is(pts, 20) is pts[1]
    # Floor empties the set → fall back to plain max-E[R].
    assert WalkForwardEngine._select_best_is(pts, 100) is pts[0]


# ── negative-caching of failed fetches (#5) ───────────────────────────────────

def test_live_price_does_not_cache_failure(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(live_price, "_fetch", lambda t: None)
    monkeypatch.setattr(live_price, "_save_cache", lambda t, p, d: saved.update({t: p}))
    assert live_price.get_live_price("AAPL", cache_dir=tmp_path) is None
    assert saved == {}                            # failure not persisted


def test_live_price_caches_success(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(live_price, "_fetch", lambda t: 123.45)
    monkeypatch.setattr(live_price, "_save_cache", lambda t, p, d: saved.update({t: p}))
    assert live_price.get_live_price("AAPL", cache_dir=tmp_path) == 123.45
    assert saved.get("AAPL") == 123.45


def test_market_cap_does_not_cache_failure(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(info_fetcher, "_fetch", lambda t: None)
    monkeypatch.setattr(info_fetcher, "save_section",
                        lambda t, sec, payload, d: saved.update({t: payload}))
    assert info_fetcher.get_market_cap("AAPL", cache_dir=tmp_path) is None
    assert saved == {}


def test_market_cap_caches_success(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(info_fetcher, "_fetch", lambda t: 1.0e9)
    monkeypatch.setattr(info_fetcher, "save_section",
                        lambda t, sec, payload, d: saved.update({t: payload}))
    assert info_fetcher.get_market_cap("AAPL", cache_dir=tmp_path) == 1.0e9
    assert saved.get("AAPL") == {"market_cap": 1.0e9}


# ── macro regime delta bugs (#4) ──────────────────────────────────────────────

def _fred(values, index):
    return pd.DataFrame({"value": list(values)}, index=index)


def test_curve_fresh_inversion_is_inverted():
    from core.macro.regime import classify_macro_state
    idx = pd.date_range("2024-01-01", periods=4, freq="MS")
    # 2 months ago spread +0.5 (not inverted); now spread −0.5 → fresh inversion.
    dgs10 = _fred([4.0, 4.0, 3.0, 3.0], idx)
    dgs3mo = _fred([3.5, 3.5, 3.5, 3.5], idx)
    st = classify_macro_state({"DGS10": dgs10, "DGS3MO": dgs3mo})
    assert st.curve_state == "INVERTED"   # was "FLAT" before the fix


def test_wcs_wide_is_reachable():
    from core.macro.regime import classify_macro_state
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    brent = _fred([90, 92, 93, 94, 95], idx)   # BZ=F proxy
    wti = _fred([78, 79, 80, 80, 80], idx)     # CL=F
    st = classify_macro_state({"BZ=F": brent, "CL=F": wti})
    # Brent−WTI = 15 > 10 → WIDE; the old `< -25` made WIDE unreachable.
    assert st.wcs_spread_state == "WIDE"


def test_inflation_constant_growth_is_stable():
    from core.macro.regime import classify_macro_state
    idx = pd.date_range("2022-01-01", periods=24, freq="MS")
    pce = _fred([100 * (1.005 ** i) for i in range(24)], idx)   # constant 0.5%/mo
    st = classify_macro_state({"PCEPILFE": pce})
    # Constant growth → true 12-mo YoY is flat → STABLE. The old 11-month iloc span
    # vs the 12-month comparison leg produced a spurious DECELERATING.
    assert st.inflation_state == "STABLE"


def test_credit_short_daily_history_rejected():
    from core.macro.regime import classify_macro_state
    idx = pd.date_range("2024-01-01", periods=100, freq="D")   # ~3 months daily
    hy = _fred(list(np.linspace(3.0, 8.0, 100)), idx)          # current = max
    st = classify_macro_state({"BAMLH0A0HYM2": hy})
    # < ~1yr of daily history → percentile path skipped → NORMAL. The old
    # `len >= 12` guard would have computed WIDE off ~100 days of data.
    assert st.credit_state == "NORMAL"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
