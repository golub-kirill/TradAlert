"""
Phase 10 v2 polish tests — asymmetric ``min_rr_short`` and the
hard-to-borrow short block list, both in ``FilterEngine._signal_entry``.

Both features are opt-in and short-only: longs and pre-v2 configs are
unaffected. Tests force the short path via stubbed regime + trigger so
they are deterministic and offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from core.filter_engine import FilterEngine, MarketRegime


def _cfg() -> dict:
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "filters.yaml")
        .read_text(encoding="utf-8")
    )
    cfg["signals"]["allow_shorts"] = True
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    return cfg


def _engine(cfg: dict, *, direction: str = "short") -> FilterEngine:
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)
    sigtype = "momentum"
    eng._evaluate_entry = lambda *a, **kw: (direction, sigtype, "forced")
    trend = "DOWNTREND" if direction == "short" else "UPTREND"
    vol_trend = "BEAR" if direction == "short" else "BULL"
    eng._market_regime = lambda md, vd: MarketRegime(trend=vol_trend, volatility="LOW")
    eng._ticker_trend = lambda d: trend
    return eng


def _df(n: int = 220) -> pd.DataFrame:
    row = {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.10,
        "ma_fast": 95.0, "ma_slow": 90.0,
    }
    return pd.DataFrame([dict(row) for _ in range(n)],
                        index=pd.date_range("2024-01-01", periods=n, freq="B"))


# ─── min_rr_short ─────────────────────────────────────────────────────────────


def test_min_rr_short_overrides_min_rr_for_shorts():
    cfg = _cfg()
    cfg["signals"]["stop_loss"]["min_rr"] = 2.5
    cfg["signals"]["stop_loss"]["min_rr_short"] = 1.5
    res = _engine(cfg).signal("ABC", _df(), market_dfs=None, vix_df=None,
                              earnings_date=None)
    assert res.passed and res.direction == "short"
    assert res.min_rr == 1.5
    close = 100.0
    stop_dist = res.stop_price - close  # atr(1) * atr_multiplier(2.5)
    target_dist = close - res.target_price
    assert abs(stop_dist - 2.5) < 1e-9
    assert abs(target_dist - stop_dist * 1.5) < 1e-9  # uses min_rr_short


def test_min_rr_short_absent_falls_back_to_min_rr():
    cfg = _cfg()
    cfg["signals"]["stop_loss"]["min_rr"] = 2.5
    cfg["signals"]["stop_loss"].pop("min_rr_short", None)
    res = _engine(cfg).signal("ABC", _df(), market_dfs=None, vix_df=None,
                              earnings_date=None)
    assert res.passed and res.min_rr == 2.5


def test_min_rr_short_does_not_touch_longs():
    cfg = _cfg()
    cfg["signals"]["stop_loss"]["min_rr"] = 2.5
    cfg["signals"]["stop_loss"]["min_rr_short"] = 1.5
    res = _engine(cfg, direction="long").signal("ABC", _df(), market_dfs=None,
                                                vix_df=None, earnings_date=None)
    assert res.passed and res.direction == "long"
    assert res.min_rr == 2.5  # long still uses min_rr


# ─── hard-to-borrow block list ────────────────────────────────────────────────


def test_htb_list_blocks_short_on_listed_symbol():
    cfg = _cfg()
    cfg["signals"]["hard_to_borrow_list"] = ["GME", "AMC"]
    res = _engine(cfg).signal("GME", _df(), market_dfs=None, vix_df=None,
                              earnings_date=None)
    assert res.passed is False
    assert "hard-to-borrow" in res.reason.lower()


def test_htb_list_allows_short_on_unlisted_symbol():
    cfg = _cfg()
    cfg["signals"]["hard_to_borrow_list"] = ["GME"]
    res = _engine(cfg).signal("ABC", _df(), market_dfs=None, vix_df=None,
                              earnings_date=None)
    assert res.passed is True and res.direction == "short"


def test_htb_list_does_not_block_longs():
    cfg = _cfg()
    cfg["signals"]["hard_to_borrow_list"] = ["GME"]
    # A long on a HTB-listed symbol must still pass — borrow only affects shorts.
    res = _engine(cfg, direction="long").signal("GME", _df(), market_dfs=None,
                                                vix_df=None, earnings_date=None)
    assert res.passed is True and res.direction == "long"


def test_htb_empty_list_is_noop():
    cfg = _cfg()
    cfg["signals"]["hard_to_borrow_list"] = []
    res = _engine(cfg).signal("GME", _df(), market_dfs=None, vix_df=None,
                              earnings_date=None)
    assert res.passed is True and res.direction == "short"
