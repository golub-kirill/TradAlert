"""Entry-gate trigger panel — engine checks, telegram factor line, chart render.

The panel is the engine's direction-aware "proof of opinion": ``SignalResult.checks``
is built *after* a signal fires (``signal(with_checks=True)``) and never alters a
decision, so the backtest/sweep path (``with_checks=False``) replays bit-identically.
These cover: the long/short factor sets flip with direction, replay-equality, a
near-miss flipping the right row, the VBP short-side helper, the Telegram group
summary line, and a headless render of the chart panel.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")  # headless render

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402

from core.filter_engine import (  # noqa: E402
    FilterEngine, GateCheck, MarketRegime, SignalResult,
)
from core.indicators.vbp import (  # noqa: E402
    nearest_high_volume_node_above, nearest_high_volume_node_below,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _engine() -> FilterEngine:
    """Engine with structural gates off so a stubbed decision reaches the panel."""
    cfg = _load_cfg()
    cfg.setdefault("signals", {})
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["signals"]["require_trigger_bar_up"] = False
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)
    return eng


def _stub(eng: FilterEngine, direction: str, signal_type: str = "momentum") -> None:
    """Force ``_evaluate_entry`` to a fixed decision; the panel reads the row data."""
    eng._evaluate_entry = lambda *a, **kw: (direction, signal_type, f"{signal_type} {direction}")


def _firing_df(*, close: float = 101.0, ma_fast: float = 95.5, ma_slow: float = 90.5,
               rsi: float = 55.0, macd_hist: float = 0.10, prev_hist: float = 0.05,
               n_warmup: int = 260) -> pd.DataFrame:
    """DataFrame past the 200-row gate, with all columns the panel reads."""
    def row(o, h, l, c, mh):
        return dict(open=o, high=h, low=l, close=c, volume=1_000_000, atr=1.0,
                    rsi=rsi, macd=0.1, macd_signal=0.05, macd_hist=mh,
                    bb_bw=3.0, bb_z=0.4, weekly_sma10=96.0,
                    ma_fast=ma_fast, ma_slow=ma_slow)

    rows = [row(99.5, 101.0, 99.0, 100.0, -0.05) for _ in range(n_warmup)]
    rows.append(row(99.0, 101.0, 99.0, 100.0, prev_hist))
    rows.append(row(99.0, close + 1.0, close - 1.0, close, macd_hist))
    return pd.DataFrame(
        rows, index=pd.date_range("2023-01-01", periods=len(rows), freq="B"))


def _by_name(checks, group, name):
    for c in checks:
        if c.group == group and c.name == name:
            return c
    return None


# ── engine: direction-aware checks ────────────────────────────────────────────

def test_long_fire_yields_directional_checks():
    eng = _engine()
    _stub(eng, "long")
    sig = eng.signal("ABC", _firing_df(), with_checks=True)

    assert sig.passed and sig.direction == "long"
    assert sig.checks, "with_checks=True must populate checks on a fired signal"
    assert {"TREND", "MOMENTUM", "RISK", "CONTEXT"} <= {c.group for c in sig.checks}

    # In an uptrend, a long's trend/location factors pass; values are surfaced.
    assert _by_name(sig.checks, "TREND", "Trend").passed is True
    assert _by_name(sig.checks, "TREND", "Px vs MA50").passed is True
    assert _by_name(sig.checks, "TREND", "Px vs MA200").passed is True
    assert _by_name(sig.checks, "MOMENTUM", "MACD hist").passed is True
    assert "101" in _by_name(sig.checks, "TREND", "Px vs MA50").detail


def test_short_fire_flips_the_same_rows():
    """Same uptrend data, opposite direction → the trend/momentum rows invert."""
    eng = _engine()
    _stub(eng, "short")
    sig = eng.signal("ABC", _firing_df(), with_checks=True)

    assert sig.passed and sig.direction == "short"
    assert _by_name(sig.checks, "TREND", "Trend").passed is False
    assert _by_name(sig.checks, "TREND", "Px vs MA50").passed is False
    assert _by_name(sig.checks, "TREND", "Px vs MA200").passed is False
    assert _by_name(sig.checks, "MOMENTUM", "MACD hist").passed is False


def test_replay_is_bit_identical_except_checks():
    eng = _engine()
    _stub(eng, "long")
    df = _firing_df()
    off = eng.signal("ABC", df, with_checks=False)
    on = eng.signal("ABC", df, with_checks=True)

    assert off.checks == []           # default path never builds checks
    assert on.checks                  # opt-in path does
    for f in fields(off):
        if f.name == "checks":
            continue
        assert getattr(off, f.name) == getattr(on, f.name), f"field {f.name} drifted"


def test_default_path_carries_no_checks():
    eng = _engine()
    _stub(eng, "long")
    sig = eng.signal("ABC", _firing_df())   # with_checks defaults to False
    assert sig.passed and sig.checks == []


def test_near_miss_flips_the_right_row():
    eng = _engine()
    _stub(eng, "long")
    # Baseline: price above MA50 → row passes.
    base = eng.signal("ABC", _firing_df(ma_fast=95.5), with_checks=True)
    assert _by_name(base.checks, "TREND", "Px vs MA50").passed is True
    # Near miss: MA50 lifted above price → only that row flips.
    miss = eng.signal("ABC", _firing_df(ma_fast=105.0), with_checks=True)
    assert _by_name(miss.checks, "TREND", "Px vs MA50").passed is False


# ── VBP short-side helper ─────────────────────────────────────────────────────

def test_vbp_nearest_node_below_mirrors_above():
    s = pd.Series({10.0: 5.0, 20.0: 100.0, 30.0: 100.0, 40.0: 5.0})
    above = nearest_high_volume_node_above(s, 25.0)
    below = nearest_high_volume_node_below(s, 25.0)
    assert above is not None and above[0] == 30.0   # nearest high-vol shelf up
    assert below is not None and below[0] == 20.0    # nearest high-vol shelf down
    assert nearest_high_volume_node_below(s, 5.0) is None   # nothing below the floor
    assert nearest_high_volume_node_below(pd.Series(dtype=float), 5.0) is None


# ── telegram factor line ──────────────────────────────────────────────────────

def test_checklist_summarizes_groups():
    from core.telegram.push import _checklist
    checks = [
        GateCheck("TREND", "a", True), GateCheck("TREND", "b", True),
        GateCheck("MOMENTUM", "x", True), GateCheck("MOMENTUM", "y", False),
        GateCheck("RISK", "r", False),
    ]
    states = dict(_checklist(SimpleNamespace(checks=checks)))
    assert states["TREND"] is True     # all pass
    assert states["MOM"] is None       # mixed
    assert states["RISK"] is False     # none pass
    assert "LOC" not in states and "VOL" not in states  # empty groups omitted


def test_checklist_empty_when_no_checks():
    from core.telegram.push import _checklist
    assert _checklist(SimpleNamespace(checks=[])) == []
    assert _checklist(SimpleNamespace(checks=None)) == []


def test_format_entry_renders_factor_line():
    from core.telegram import format as fmt
    tr = SimpleNamespace(
        ticker="ABC",
        signal=SimpleNamespace(
            direction="long", signal_type="momentum", target_price=110.0,
            stop_price=95.0, min_rr=2.5, size_mult=1.0,
            expected_hold_days=(10, 15), market_regime="BULL_NORMAL"),
        scan=SimpleNamespace(close=100.0),
    )
    text = fmt.format_entry(tr, checklist=[("TREND", True), ("MOM", None), ("RISK", False)])
    assert "🔎" in text
    assert "TREND ✅" in text and "MOM ▫️" in text and "RISK ❌" in text


# ── live risk-budget + size_mult surfacing ────────────────────────────────────

def test_panel_includes_size_row():
    eng = _engine()
    _stub(eng, "long")
    sig = eng.signal("ABC", _firing_df(), with_checks=True)
    size = _by_name(sig.checks, "RISK", "Size")
    assert size is not None and size.detail.endswith("x")   # size_mult surfaced


def test_live_context_budget_row_flags_over_budget():
    from main import _append_live_context_checks
    over = SimpleNamespace(direction="long", checks=[GateCheck("TREND", "x", True)])
    _append_live_context_checks(over, ticker_rp=80, n_open=6, max_open_risk=5.0)
    b = next(c for c in over.checks if c.group == "CONTEXT" and c.name == "Budget")
    assert b.passed is False and "6/5" in b.detail        # 6 open ≥ 5R → over budget
    rp = next(c for c in over.checks if c.group == "LOCATION" and c.name == "RP")
    assert rp.passed is True                                # RP 80 (long) is strong

    under = SimpleNamespace(direction="long", checks=[GateCheck("TREND", "x", True)])
    _append_live_context_checks(under, ticker_rp=10, n_open=2, max_open_risk=5.0)
    b2 = next(c for c in under.checks if c.name == "Budget")
    assert b2.passed is True                                # 2 < 5R → room


# ── chart render (headless) ───────────────────────────────────────────────────

@pytest.mark.parametrize("direction", ["long", "short"])
def test_chart_renders_trigger_panel(tmp_path, direction):
    from core.indicators.chart import chart
    eng = _engine()
    _stub(eng, direction)
    sig = eng.signal("ABC", _firing_df(), with_checks=True)
    assert sig.checks
    regime = MarketRegime(trend="BULL", volatility="NORMAL")
    out = chart("ABC", _firing_df(), signal=sig, output_dir=tmp_path, regime=regime)
    assert out.exists() and out.stat().st_size > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
