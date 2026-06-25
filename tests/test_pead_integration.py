"""Engine WIRING tests for the PEAD long branch in ``FilterEngine._signal_entry``.

The PEAD entry math lives in ``core.pead.qualifies`` and is unit-tested in
``tests/test_pead.py``. This file guards only the *wiring* around it: the
``signals.pead.enabled`` flag, the regime kill-switch, the gap-risk / anti-gap
bypass, the long tag + stop/target geometry, and the fall-through to the
existing momentum path when PEAD does not fire.

To isolate the wiring from PEAD data-construction complexity, ``qualifies`` is
monkeypatched (patched in the module that USES it: ``core.filter_engine``) to a
deterministic verdict, and a precomputed BULL regime is passed via ``regime=``
so the internal regime computation is short-circuited (signal() ~343-348).

The BULL regime is a real ``MarketRegime`` (re-exported by ``core.filter_engine``,
same import idiom as ``tests/test_short_v2.py``) — it exercises the genuine
``allows_longs`` / ``label`` / ``size_multiplier`` properties the branch reads,
which is more robust than a hand-rolled namespace.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from core.filter_engine import EarningsEvent, FilterEngine, MarketRegime

# Real regimes: BULL_NORMAL allows longs (size_mult 1.0), BEAR_NORMAL does not.
BULL = MarketRegime(trend="BULL", volatility="NORMAL")
NON_BULL = MarketRegime(trend="BEAR", volatility="NORMAL")


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _cfg(*, pead_enabled: bool, gap_risk: bool = False) -> dict:
    """filters.yaml with the PEAD flag forced and unrelated gates off."""
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "filters.yaml")
        .read_text(encoding="utf-8")
    )
    cfg["signals"].setdefault("pead", {})
    cfg["signals"]["pead"]["enabled"] = pead_enabled
    cfg["signals"]["gap_risk"] = {"enabled": gap_risk, "max_prev_bar_range_atr": 3.0}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    return cfg


def _engine(cfg: dict) -> FilterEngine:
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)  # arbitrary non-blackout date
    return eng


def _quiet_df(*, prev_range: float = 2.0, n: int = 220) -> pd.DataFrame:
    """A flat df whose last bar does NOT itself trigger a momentum/MR long.

    rsi=45 is below the momentum band (50-70) and above the mean-rev ceiling
    (30), and macd_hist is flat (no zero-cross, zero delta) — so the only way
    to a long is via PEAD. ``prev_range`` sets the previous bar's high-low span
    (in price units) to exercise the gap-risk gate; atr is 1.0 throughout.
    """
    rows = [
        dict(open=100.0, high=101.0, low=99.0, close=100.0,
             volume=1_000_000.0, atr=1.0, rsi=45.0,
             macd=0.0, macd_signal=0.0, macd_hist=0.0,
             ma_fast=95.0, ma_slow=90.0)
        for _ in range(n)
    ]
    # prev bar (iloc[-2]) carries the configurable range for the gap-risk test.
    half = prev_range / 2.0
    rows[-2] = dict(open=100.0, high=100.0 + half, low=100.0 - half, close=100.0,
                    volume=1_000_000.0, atr=1.0, rsi=45.0,
                    macd=0.0, macd_signal=0.0, macd_hist=0.0,
                    ma_fast=95.0, ma_slow=90.0)
    return pd.DataFrame(
        rows, index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def _spy_df() -> dict[str, pd.DataFrame]:
    """Minimal market_dfs satisfying the PEAD guard's SPY['close'] precondition."""
    spy = pd.DataFrame(
        {"close": [400.0] * 220},
        index=pd.date_range("2024-01-01", periods=220, freq="B"),
    )
    return {"SPY": spy}


def _events() -> list[EarningsEvent]:
    """Non-empty earnings_events so the PEAD guard's precondition is met.

    The monkeypatched ``qualifies`` ignores these — only its presence matters.
    """
    return [EarningsEvent(date=date(2024, 11, 1), session="BMO")]


# ─── 1. flag gates the branch ─────────────────────────────────────────────────


def test_pead_off_ignores_events(monkeypatch):
    """pead DISABLED: even with qualifies→True + events + SPY supplied, the
    branch is inert and the result is NOT pead-tagged."""
    monkeypatch.setattr("core.filter_engine.qualifies",
                        lambda *a, **k: (True, 0.05, "x"))
    eng = _engine(_cfg(pead_enabled=False))
    res = eng.signal("ABC", _quiet_df(), market_dfs=_spy_df(), vix_df=None,
                     earnings_date=None, earnings_events=_events(), regime=BULL)
    assert res.signal_type != "pead"


# ─── 2. fires, tagged, and carries valid long geometry ────────────────────────


def test_pead_fires_and_is_tagged(monkeypatch):
    """pead ENABLED + qualifies→True + BULL regime on a df that would NOT
    trigger a momentum long → the PEAD long fires, is tagged, and applies the
    2.5-ATR / 2.5 R:R long geometry."""
    monkeypatch.setattr("core.filter_engine.qualifies",
                        lambda *a, **k: (True, 0.05, "strong"))
    eng = _engine(_cfg(pead_enabled=True))
    res = eng.signal("ABC", _quiet_df(), market_dfs=_spy_df(), vix_df=None,
                     earnings_date=None, earnings_events=_events(), regime=BULL)
    assert res.passed is True
    assert res.direction == "long"
    assert res.signal_type == "pead"
    assert res.stop_price > 0
    assert res.target_price > res.stop_price


# ─── 3. bypasses gap-risk (the earnings gap IS the signal) ────────────────────


def test_pead_bypasses_gap_risk(monkeypatch):
    """pead ENABLED + gap_risk ENABLED: a WIDE previous bar that blocks a normal
    entry does NOT block the PEAD long. Control: with PEAD off (qualifies→False)
    the same wide-bar df is blocked by gap-risk."""
    # prev bar range 8.0 > 3.0 * atr(1.0) → would trip the gap-risk gate.
    wide_df = _quiet_df(prev_range=8.0)

    # PEAD on → bypasses gap-risk and fires.
    monkeypatch.setattr("core.filter_engine.qualifies",
                        lambda *a, **k: (True, 0.05, "strong"))
    eng = _engine(_cfg(pead_enabled=True, gap_risk=True))
    res = eng.signal("ABC", wide_df, market_dfs=_spy_df(), vix_df=None,
                     earnings_date=None, earnings_events=_events(), regime=BULL)
    assert res.passed is True
    assert res.signal_type == "pead"

    # Control: PEAD off → gap-risk gate blocks the same wide-bar df.
    eng_off = _engine(_cfg(pead_enabled=False, gap_risk=True))
    res_off = eng_off.signal("ABC", wide_df, market_dfs=_spy_df(), vix_df=None,
                             earnings_date=None, earnings_events=_events(),
                             regime=BULL)
    assert res_off.passed is False
    assert "ATR" in res_off.reason or "range" in res_off.reason.lower()


# ─── 4. regime kill-switch still applies ──────────────────────────────────────


def test_pead_respects_regime_killswitch(monkeypatch):
    """pead ENABLED + qualifies→True but a NON-bull regime (allows_longs=False)
    → the branch's ``regime.allows_longs`` guard blocks PEAD (no pead long)."""
    monkeypatch.setattr("core.filter_engine.qualifies",
                        lambda *a, **k: (True, 0.05, "strong"))
    eng = _engine(_cfg(pead_enabled=True))
    res = eng.signal("ABC", _quiet_df(), market_dfs=_spy_df(), vix_df=None,
                     earnings_date=None, earnings_events=_events(), regime=NON_BULL)
    assert res.signal_type != "pead"


# ─── 5. PEAD-not-firing falls through to the existing momentum path ───────────


def test_pead_false_falls_through_to_momentum(monkeypatch):
    """pead ENABLED but qualifies→False on a df that DOES satisfy a momentum
    long → the existing momentum entry fires unchanged.

    The momentum decision is stubbed via ``_evaluate_entry`` (the established
    idiom in test_anti_gap_entry / test_short_v2): this test guards that a
    False PEAD verdict leaves the existing entry path intact, not the momentum
    math (covered elsewhere)."""
    monkeypatch.setattr("core.filter_engine.qualifies",
                        lambda *a, **k: (False, -0.02, "no"))
    eng = _engine(_cfg(pead_enabled=True))
    # Force the downstream decision to a momentum long; require_trigger_bar_up
    # is off in production filters.yaml, so the flat-bar df does not block it.
    eng._evaluate_entry = lambda *a, **k: ("long", "momentum", "momentum long")
    res = eng.signal("ABC", _quiet_df(), market_dfs=_spy_df(), vix_df=None,
                     earnings_date=None, earnings_events=_events(), regime=BULL)
    assert res.passed is True
    assert res.direction == "long"
    assert res.signal_type == "momentum"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
