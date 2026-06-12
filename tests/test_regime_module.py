"""core.regime — the extracted MarketRegime classifier as a pure function.

Locks the classification rules directly (no engine needed), the engine
delegation parity, and the dependency direction (core.regime must never
import the engine back — that arrow is the point of the extraction).
"""

from pathlib import Path

import pandas as pd
import pytest

import core.regime as regime_mod
from core.regime import MarketRegime, classify_market_regime


def _df(closes) -> pd.DataFrame:
    return pd.DataFrame({"close": [float(c) for c in closes]})


def _cfg(**regime_keys) -> dict:
    return {"trend": {"ma_fast": 3}, "regime": regime_keys}


def _indices(spy, qqq) -> dict:
    return {"SPY": _df(spy), "QQQ": _df(qqq)}


# ── trend ─────────────────────────────────────────────────────────────────────

def test_all_indices_up_is_bull():
    r = classify_market_regime(_cfg(), _indices([1, 1, 1, 2], [1, 1, 1, 2]), None)
    assert r.trend == "BULL" and r.volatility == "NORMAL"


def test_split_votes_require_all_is_chop():
    r = classify_market_regime(_cfg(require_all_indices=True),
                               _indices([1, 1, 1, 2], [2, 2, 2, 1]), None)
    assert r.trend == "CHOP"


def test_majority_mode_breaks_ties_toward_winner():
    cfg = _cfg(require_all_indices=False, index_symbols=["SPY", "QQQ", "IWM"])
    dfs = {"SPY": _df([1, 1, 1, 2]), "QQQ": _df([1, 1, 1, 2]),
           "IWM": _df([2, 2, 2, 1])}
    assert classify_market_regime(cfg, dfs, None).trend == "BULL"


def test_all_down_is_bear():
    r = classify_market_regime(_cfg(), _indices([2, 2, 2, 1], [2, 2, 2, 1]), None)
    assert r.trend == "BEAR"


def test_missing_index_data_defaults_to_chop_and_logs(caplog):
    with caplog.at_level("ERROR"):
        r = classify_market_regime(_cfg(), None, None)
    assert r.trend == "CHOP"
    assert not r.allows_longs
    assert "no index data" in caplog.text


def test_ma_short_misalignment_demotes_bull_to_chop():
    cfg = _cfg(require_ma_short_alignment=True, ma_short=2)
    # last 6 > MA(3)=5.67 (BULL vote) but < MA(2)=8 → demoted
    dfs = _indices([1, 1, 10, 6], [1, 1, 10, 6])
    assert classify_market_regime(cfg, dfs, None).trend == "CHOP"
    # same shape without the gate stays BULL
    assert classify_market_regime(_cfg(), dfs, None).trend == "BULL"


# ── volatility + slope ────────────────────────────────────────────────────────

def test_vix_bands():
    dfs = _indices([1, 1, 1, 2], [1, 1, 1, 2])
    cfg = _cfg(vix_low=15, vix_high=25)
    assert classify_market_regime(cfg, dfs, _df([10])).volatility == "LOW"
    assert classify_market_regime(cfg, dfs, _df([20])).volatility == "NORMAL"
    assert classify_market_regime(cfg, dfs, _df([30])).volatility == "HIGH"
    assert classify_market_regime(cfg, dfs, None).volatility == "NORMAL"


def test_vix_rising_uses_slope_lookback():
    dfs = _indices([1, 1, 1, 2], [1, 1, 1, 2])
    cfg = _cfg(vix_low=15, vix_high=25, vix_slope_lookback_days=2)
    rising = classify_market_regime(cfg, dfs, _df([18, 17, 16, 19]))
    flat = classify_market_regime(cfg, dfs, _df([19, 18, 17, 16]))
    assert rising.vix_rising is True
    assert flat.vix_rising is False
    # series shorter than the lookback: defensive False
    short = classify_market_regime(cfg, dfs, _df([18, 19]))
    assert short.vix_rising is False


# ── seam guarantees ───────────────────────────────────────────────────────────

def test_engine_delegation_parity():
    import yaml
    from core.filter_engine import FilterEngine
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    eng = FilterEngine.from_dict(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))
    n = eng._cfg["trend"]["ma_fast"] + 1
    dfs = _indices([100.0] * (n - 1) + [110.0], [100.0] * (n - 1) + [110.0])
    vix = _df([18, 17, 16, 19])
    assert eng.market_regime(dfs, vix) == classify_market_regime(eng._cfg, dfs, vix)


def test_regime_module_does_not_import_the_engine():
    src = Path(regime_mod.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]          # skip the module docstring
    assert "filter_engine" not in body


def test_types_module_is_a_leaf():
    import core.types as types_mod
    src = Path(types_mod.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]          # skip the module docstring
    assert "filter_engine" not in body


def test_reexports_are_the_same_objects():
    import core.filter_engine as fe
    assert fe.MarketRegime is MarketRegime
    assert fe.classify_market_regime is classify_market_regime


def test_size_multiplier_composite():
    class _Axis:
        def __init__(self, m):
            self.size_multiplier = m

    r = MarketRegime(trend="BULL", volatility="LOW",
                     macro=_Axis(0.5), behavioral=_Axis(0.5))
    assert r.size_multiplier == pytest.approx(0.5)
    assert MarketRegime(trend="BULL", volatility="LOW",
                        macro=_Axis(0.0)).size_multiplier == 0.0
    assert MarketRegime(trend="BULL", volatility="LOW").size_multiplier == 1.0
