"""Held-long regime-flip exit variants (A/B levers).

Covers the two config-gated shaping knobs added for the chop-exit A/B:
    signals.exits.regime_flip_bear_only    — exit only on BEAR (ignore CHOP)
    signals.exits.regime_flip_confirm_bars — require the flip to persist N bars

Defaults (bear_only=False, confirm_bars=1) must reproduce the original
"exit on any non-BULL bar" behavior exactly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yaml

import core.filter_engine as fe
from core.filter_engine import FilterEngine, MarketRegime


def _load_cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _engine(exits: dict | None = None) -> FilterEngine:
    cfg = _load_cfg()
    if exits is not None:
        cfg["signals"]["exits"] = exits
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)
    # Isolate the regime branch: neutralise row-guard, trend label, and the two
    # non-regime exit triggers so only the regime-flip logic can fire.
    eng._min_rows_guard = lambda *a, **k: None
    eng._ticker_trend = lambda df: "UPTREND"
    eng._momentum_fade_exit = lambda *a, **k: False
    eng._mean_rev_exit = lambda *a, **k: False
    return eng


def _df() -> pd.DataFrame:
    return pd.DataFrame([{"close": 100.0}, {"close": 101.0}])


# ── baseline (byte-identical behavior) ───────────────────────────────────────

def test_baseline_chop_fires_regime_exit():
    eng = _engine()
    res = eng._signal_exit("X", _df(), MarketRegime(trend="CHOP", volatility="LOW"))
    assert res.direction == "exit_long" and res.signal_type == "regime"


def test_baseline_bull_holds():
    eng = _engine()
    res = eng._signal_exit("X", _df(), MarketRegime(trend="BULL", volatility="LOW"))
    assert res.direction != "exit_long"


# ── variant C: BEAR-only ─────────────────────────────────────────────────────

def test_bear_only_ignores_chop():
    eng = _engine({"regime_flip_bear_only": True})
    res = eng._signal_exit("X", _df(), MarketRegime(trend="CHOP", volatility="LOW"))
    assert res.direction != "exit_long"  # CHOP no longer flattens


def test_bear_only_still_exits_on_bear():
    eng = _engine({"regime_flip_bear_only": True})
    res = eng._signal_exit("X", _df(), MarketRegime(trend="BEAR", volatility="LOW"))
    assert res.direction == "exit_long" and res.signal_type == "regime"


# ── variant B: N-bar confirmation ────────────────────────────────────────────

def test_unconfirmed_flip_holds():
    eng = _engine()
    res = eng._signal_exit("X", _df(), MarketRegime(trend="CHOP", volatility="LOW"),
                           regime_confirmed=False)
    assert res.direction != "exit_long"  # flip not yet persistent → hold


def test_confirmed_flip_fires():
    eng = _engine()
    res = eng._signal_exit("X", _df(), MarketRegime(trend="CHOP", volatility="LOW"),
                           regime_confirmed=True)
    assert res.direction == "exit_long"


def test_confirm_helper_requires_streak(monkeypatch):
    eng = _engine()
    mkt = {"SPY": pd.DataFrame({"close": range(10)})}
    # Trend keyed by remaining length after truncation: k=1 → len 9, k=2 → len 8.
    trends = {9: "CHOP", 8: "BULL"}
    monkeypatch.setattr(fe, "classify_market_regime",
                        lambda cfg, m, v, *a, **k: MarketRegime(
                            trend=trends[len(m["SPY"])], volatility="LOW"))
    # confirm_bars=2 checks only k=1 (CHOP) → confirmed.
    assert eng._regime_flip_confirmed(mkt, None, 2, False) is True
    # confirm_bars=3 also checks k=2 (BULL) → streak broken → not confirmed.
    assert eng._regime_flip_confirmed(mkt, None, 3, False) is False


def test_confirm_helper_bear_only_rejects_chop(monkeypatch):
    eng = _engine()
    mkt = {"SPY": pd.DataFrame({"close": range(10)})}
    monkeypatch.setattr(fe, "classify_market_regime",
                        lambda cfg, m, v, *a, **k: MarketRegime(trend="CHOP", volatility="LOW"))
    # bear_only: a prior CHOP bar is not flip-worthy → not confirmed.
    assert eng._regime_flip_confirmed(mkt, None, 2, True) is False


def test_confirm_helper_noop_at_one_bar():
    eng = _engine()
    assert eng._regime_flip_confirmed({"SPY": pd.DataFrame({"close": [1]})}, None, 1, False) is True
