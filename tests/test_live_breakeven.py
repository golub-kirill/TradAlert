"""
Live breakeven stop-raise (main._maybe_raise_stop_to_breakeven).

The live scan must mirror the backtester's breakeven rule (shared decision in
core.exits.breakeven_stop_level): once a held long's best excursion since
entry reaches execution.breakeven_trigger_r, positions.stop_price moves to
entry — UP only, idempotent across scans, initial_stop untouched.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from main import _maybe_raise_stop_to_breakeven

EXEC_CFG = SimpleNamespace(breakeven_trigger_r=1.0, breakeven_buffer_atr=None)


def _df(highs, lows=None, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(highs))
    if lows is None:
        lows = [h - 1.0 for h in highs]
    return pd.DataFrame({"high": highs, "low": lows,
                         "close": [h - 0.5 for h in highs]}, index=idx)


def _pos(stop_price=95.0, initial_stop=95.0, side="long", entry_bar_of=None):
    entry_date = (entry_bar_of.index[0].date()
                  if entry_bar_of is not None else date(2026, 1, 5))
    return SimpleNamespace(id=7, ticker="TEST.1", side=side,
                           entry_price=100.0, entry_date=entry_date,
                           stop_price=stop_price, initial_stop=initial_stop)


def _short_pos(stop_price=110.0, initial_stop=110.0):
    """Held short: entry 100, initial stop ABOVE entry (risk = stop − entry = 10)."""
    return SimpleNamespace(id=8, ticker="TEST.2", side="short",
                           entry_price=100.0, entry_date=date(2026, 1, 5),
                           stop_price=stop_price, initial_stop=initial_stop)


@pytest.fixture
def calls(monkeypatch):
    rec = {"update": [], "notice": []}
    import core.position_manager as pm
    import core.telegram.push as push
    monkeypatch.setattr(pm, "update_stop",
                        lambda pid, stop: rec["update"].append((pid, stop)) or True)
    monkeypatch.setattr(push, "send_notice",
                        lambda text, settings: rec["notice"].append(text))
    return rec


def test_stop_raised_to_entry_when_mfe_reaches_trigger(calls):
    df = _df([101, 103, 106, 104])  # MFE = (106-100)/5 = 1.2R ≥ 1.0R
    new = _maybe_raise_stop_to_breakeven("TEST.1", df, _pos(), EXEC_CFG, {})
    assert new == 100.0
    assert calls["update"] == [(7, 100.0)]
    assert len(calls["notice"]) == 1


def test_untouched_below_trigger(calls):
    df = _df([101, 103, 104])  # MFE = 0.8R < 1.0R
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, _pos(), EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_never_lowers_a_stop_already_above_entry(calls):
    df = _df([101, 106, 108])
    pos = _pos(stop_price=101.0)  # manually trailed above entry
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, pos, EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_idempotent_across_scans(calls):
    df = _df([101, 106, 104])
    pos = _pos()
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, pos, EXEC_CFG, {}) == 100.0
    pos.stop_price = 100.0  # what the first scan wrote
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, pos, EXEC_CFG, {}) is None
    assert len(calls["update"]) == 1
    assert len(calls["notice"]) == 1


def test_legacy_row_falls_back_to_stop_price_denominator(calls):
    df = _df([101, 106, 104])
    pos = _pos(initial_stop=None, stop_price=95.0)  # pre-initial_stop row
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, pos, EXEC_CFG, {}) == 100.0


def test_disabled_when_trigger_absent_or_zero(calls):
    df = _df([101, 110])
    assert _maybe_raise_stop_to_breakeven(
        "TEST.1", df, _pos(),
        SimpleNamespace(breakeven_trigger_r=None, breakeven_buffer_atr=None), {}) is None
    assert _maybe_raise_stop_to_breakeven(
        "TEST.1", df, _pos(),
        SimpleNamespace(breakeven_trigger_r=0, breakeven_buffer_atr=None), {}) is None
    assert calls["update"] == []


def test_short_stop_lowered_to_entry_when_mfe_reaches_trigger(calls):
    # entry 100, initial_stop 110 → risk 10; lowest low 89 → MFE (100−89)/10 = 1.1R
    df = _df(highs=[101, 99, 96, 95], lows=[99, 95, 89, 92])
    new = _maybe_raise_stop_to_breakeven("TEST.2", df, _short_pos(), EXEC_CFG, {})
    assert new == 100.0
    assert calls["update"] == [(8, 100.0)]
    assert len(calls["notice"]) == 1


def test_short_untouched_below_trigger(calls):
    # lowest low 95 → MFE (100−95)/10 = 0.5R < 1.0R
    df = _df(highs=[101, 99, 98], lows=[99, 96, 95])
    assert _maybe_raise_stop_to_breakeven("TEST.2", df, _short_pos(), EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_short_never_raises_a_stop_already_below_entry(calls):
    # MFE ≥ trigger, but the stop is already locked in profit (below entry) — moving
    # it back up to entry would loosen risk, so it must be a no-op.
    df = _df(highs=[101, 99, 96], lows=[99, 90, 89])
    pos = _short_pos(stop_price=99.0)
    assert _maybe_raise_stop_to_breakeven("TEST.2", df, pos, EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_short_invalid_stop_below_entry_is_noop(calls):
    # A short whose initial stop sits BELOW entry has non-positive risk — skip.
    df = _df(highs=[101, 99], lows=[99, 90])
    pos = _short_pos(stop_price=95.0, initial_stop=95.0)
    assert _maybe_raise_stop_to_breakeven("TEST.2", df, pos, EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_pre_entry_highs_do_not_count(calls):
    # A spike BEFORE the entry bar must not trigger breakeven.
    df = _df([200, 101, 102, 104])
    pos = _pos()
    pos.entry_date = df.index[1].date()  # entered on the second bar
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, pos, EXEC_CFG, {}) is None
    assert calls["update"] == []


def test_failed_db_update_returns_none(monkeypatch):
    import core.position_manager as pm
    monkeypatch.setattr(pm, "update_stop", lambda pid, stop: False)
    df = _df([101, 106])
    assert _maybe_raise_stop_to_breakeven("TEST.1", df, _pos(), EXEC_CFG, {}) is None
