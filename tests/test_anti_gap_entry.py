"""
Tests for the anti-gap entry confirmation gate.

The gate fires inside ``FilterEngine._signal_entry`` after
``_evaluate_entry`` returns a long signal: if the trigger bar T closed
below its own open (red bar), the T+1 entry is blocked. Targets the 11
fast stop-outs from the 2026-05-27 postmortem (1-3 bars held, -12.9R
total, all firing on red bars).

This test exercises the gate at the engine level by monkey-patching the
underlying ``_evaluate_entry`` to return a forced long signal, then
varying only the trigger-bar OHLC. That isolates the gate from the
rest of the entry pipeline.

Run with::

    pytest tests/test_anti_gap_entry.py -v
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from core.filter_engine import FilterEngine


# ─── helpers ─────────────────────────────────────────────────────────────────


def _load_cfg() -> dict:
    """Production filters.yaml + the gate forced on for tests."""
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _engine(require_trigger_up: bool) -> FilterEngine:
    cfg = _load_cfg()
    cfg.setdefault("signals", {})["require_trigger_bar_up"] = require_trigger_up
    # Disable other gates that might interfere with the test.
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)  # arbitrary non-blackout date
    return eng


def _stub_long_signal(eng: FilterEngine, signal_type: str = "momentum") -> None:
    """Force ``_evaluate_entry`` to return a long of ``signal_type``."""
    eng._evaluate_entry = lambda *a, **kw: ("long", signal_type, f"{signal_type} long")


def _make_df(trigger_open: float, trigger_close: float, *, n_warmup: int = 220) -> pd.DataFrame:
    """DataFrame long enough to clear ``trend.ma_slow=200`` row gate.

    Trailing two rows are the prev (neutral) and trigger (with given OC).
    All warmup rows are flat at 100 to keep indicators stable; the test
    monkey-patches ``_evaluate_entry`` anyway so indicator values don't
    matter, only OHLC on the last row.
    """
    rows = []
    for _ in range(n_warmup):
        rows.append(dict(open=100.0, high=101.0, low=99.0, close=100.0,
                         volume=1_000_000, atr=1.0, rsi=55.0,
                         macd=0.1, macd_signal=0.05, macd_hist=0.05,
                         ma_fast=95.0, ma_slow=90.0))
    rows.append(dict(open=99.0, high=101.0, low=98.0, close=100.0,
                     volume=1_000_000, atr=1.0, rsi=55.0,
                     macd=0.1, macd_signal=0.05, macd_hist=0.05,
                     ma_fast=95.5, ma_slow=90.5))
    rows.append(dict(open=trigger_open,
                     high=max(trigger_open, trigger_close) + 1.0,
                     low=min(trigger_open, trigger_close) - 1.0,
                     close=trigger_close,
                     volume=1_000_000, atr=1.0, rsi=55.0,
                     macd=0.2, macd_signal=0.10, macd_hist=0.10,
                     ma_fast=95.5, ma_slow=90.5))
    return pd.DataFrame(
        rows, index=pd.date_range("2024-01-01", periods=len(rows), freq="B"),
    )


# ─── gate behaviour ──────────────────────────────────────────────────────────


def test_gate_off_lets_red_bar_through():
    """Baseline behavior: when require_trigger_bar_up=False the gate is a no-op."""
    eng = _engine(require_trigger_up=False)
    _stub_long_signal(eng, "momentum")
    df = _make_df(trigger_open=102.0, trigger_close=99.0)  # red bar
    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is True
    assert result.direction == "long"


def test_gate_blocks_red_trigger_bar_momentum():
    eng = _engine(require_trigger_up=True)
    _stub_long_signal(eng, "momentum")
    df = _make_df(trigger_open=102.0, trigger_close=99.0)  # close < open
    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is False
    assert "trigger bar red" in result.reason.lower() or "anti-gap" in result.reason.lower()


def test_gate_blocks_red_trigger_bar_mean_reversion():
    """Gate applies to mean-reversion too — postmortem flagged both signal types."""
    eng = _engine(require_trigger_up=True)
    _stub_long_signal(eng, "mean_reversion")
    df = _make_df(trigger_open=102.0, trigger_close=99.0)
    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is False


def test_gate_allows_green_trigger_bar():
    """Trigger bar closed above its open → entry queued."""
    eng = _engine(require_trigger_up=True)
    _stub_long_signal(eng, "momentum")
    df = _make_df(trigger_open=99.0, trigger_close=101.0)  # green bar
    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is True
    assert result.direction == "long"


def test_gate_allows_doji_trigger_bar():
    """Close == open: gate allows entry (we require close < open to block)."""
    eng = _engine(require_trigger_up=True)
    _stub_long_signal(eng, "momentum")
    df = _make_df(trigger_open=100.0, trigger_close=100.0)
    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
