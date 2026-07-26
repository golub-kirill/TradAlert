"""Stop geometry on rows ALREADY in the book.

``validate_open`` guards the open action; nothing re-checked existing rows, so a
corrupt stop (live case: TGT id=5, a long stop at 395.74 on a 132.64 entry —
above the instrument's all-time high) sat in the book silently disabling the
breakeven ratchet. The guard must catch that WITHOUT flagging the legitimate
at-or-above-entry stops that the breakeven ratchet and manual profit-locks
produce — 7 of 19 journaled rows carry one.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

import main
from core.position_manager import stop_geometry_problems


def _pos(**over):
    base = dict(id=1, ticker="TEST.1", side="long", entry_price=100.0,
                entry_date=date(2026, 6, 2), stop_price=95.0, initial_stop=95.0)
    base.update(over)
    return SimpleNamespace(**base)


# ── the corrupt case ──────────────────────────────────────────────────────────

def test_unreachable_long_stop_is_flagged():
    """The live TGT shape: stop far above anything the market has traded."""
    problems = stop_geometry_problems(
        _pos(entry_price=132.64, stop_price=395.74, initial_stop=123.0634),
        high_since_entry=144.40, low_since_entry=120.77)
    assert len(problems) == 1
    assert "unreachable" in problems[0]


def test_unreachable_short_stop_is_flagged():
    problems = stop_geometry_problems(
        _pos(side="short", entry_price=100.0, stop_price=60.0, initial_stop=105.0),
        high_since_entry=104.0, low_since_entry=88.0)
    assert len(problems) == 1 and "unreachable" in problems[0]


def test_inverted_initial_stop_is_flagged():
    """initial_stop is the R denominator — inverted, realized-R is meaningless."""
    problems = stop_geometry_problems(_pos(initial_stop=105.0, stop_price=95.0),
                                      high_since_entry=120.0, low_since_entry=90.0)
    assert len(problems) == 1 and "R denominator" in problems[0]


# ── the legitimate cases the naive risk_unit()<=0 rule would false-positive ────

def test_breakeven_stop_at_entry_is_clean():
    """ATD.TO/ITA/IJR/CAT shape: ratchet moved the stop exactly to entry."""
    assert stop_geometry_problems(_pos(stop_price=100.0, initial_stop=95.0),
                                  high_since_entry=115.0, low_since_entry=94.0) == []


def test_profit_locked_stop_above_entry_is_clean():
    """LRCX shape: stop ratcheted to 433.33 over a 362.52 entry, inside the
    438.50 high since entry — and the position later exited at exactly that stop."""
    assert stop_geometry_problems(
        _pos(entry_price=362.52, stop_price=433.33, initial_stop=309.816),
        high_since_entry=438.50, low_since_entry=298.65) == []


def test_legacy_row_without_initial_stop_is_not_flagged():
    """Rows predating the initial_stop column carry None and fall back to
    stop_price. That fallback is live and ratchetable — checking it here would
    flag every legacy position the breakeven move has since pushed to entry."""
    assert stop_geometry_problems(_pos(initial_stop=None, stop_price=100.0),
                                  high_since_entry=115.0, low_since_entry=94.0) == []
    # …and further, a legacy row profit-locked above entry stays clean.
    assert stop_geometry_problems(_pos(initial_stop=None, stop_price=112.0),
                                  high_since_entry=115.0, low_since_entry=94.0) == []
    # The reachability half still bites on a legacy row.
    assert len(stop_geometry_problems(_pos(initial_stop=None, stop_price=400.0),
                                      high_since_entry=115.0, low_since_entry=94.0)) == 1


def test_breached_stop_is_not_corrupt():
    """Price gapped through the stop and the exit alert has not been acted on —
    an operational state, not a data defect."""
    assert stop_geometry_problems(_pos(stop_price=95.0, initial_stop=95.0),
                                  high_since_entry=101.0, low_since_entry=88.0) == []


def test_missing_excursion_bounds_runs_geometry_only():
    """Without bounds the reachability half is skipped, not guessed."""
    assert stop_geometry_problems(_pos(stop_price=395.74)) == []
    assert len(stop_geometry_problems(_pos(initial_stop=105.0))) == 1


def test_non_positive_stop_is_flagged():
    assert any("must be > 0" in p
               for p in stop_geometry_problems(_pos(stop_price=0.0)))


# ── wiring: the scan actually runs it ─────────────────────────────────────────

def _bars(high: float, low: float, n: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2026-06-01", periods=n, freq="B")
    return pd.DataFrame({"open": low, "high": high, "low": low,
                         "close": low, "volume": 1e6}, index=idx)


def test_scan_warns_on_a_corrupt_row(caplog):
    with caplog.at_level("WARNING"):
        problems = main._warn_stop_geometry(
            "TGT", _bars(144.40, 120.77),
            _pos(id=5, ticker="TGT", entry_price=132.64, stop_price=395.74,
                 initial_stop=123.0634))
    assert len(problems) == 1
    assert any("CORRUPT open position (id=5)" in r.message for r in caplog.records)


def test_scan_is_silent_on_a_healthy_row(caplog):
    with caplog.at_level("WARNING"):
        problems = main._warn_stop_geometry("KO", _bars(101.0, 94.0), _pos(ticker="KO"))
    assert problems == []
    assert not [r for r in caplog.records if "CORRUPT" in r.message]


def test_scan_guard_never_raises_on_a_bad_frame():
    """Fail-open: a frame with no usable bars must not break the scan."""
    assert main._warn_stop_geometry("KO", pd.DataFrame(), _pos()) == []
    assert main._warn_stop_geometry("KO", None, _pos()) == []


def test_bounds_skipped_when_the_frame_starts_after_entry():
    """A frame beginning after the entry date would make iloc[0:] span bars from
    BEFORE the position existed — inflating the excursion and hiding a corrupt
    stop. The reachability half must be dropped, not guessed."""
    late = _bars(144.40, 120.77)                      # starts 2026-06-01
    pos = _pos(id=5, ticker="TGT", entry_price=132.64, stop_price=395.74,
               initial_stop=123.0634, entry_date=date(2026, 5, 1))   # before it
    assert main._warn_stop_geometry("TGT", late, pos) == []
    # Same row, entry inside the frame → the corruption is caught.
    assert len(main._warn_stop_geometry(
        "TGT", late, _pos(id=5, ticker="TGT", entry_price=132.64, stop_price=395.74,
                          initial_stop=123.0634))) == 1


def test_positions_outside_the_scan_universe_are_still_checked(monkeypatch):
    """A pruned or delisted symbol never reaches _process_ticker, and that is
    exactly where a corrupt row hides. _run_pipeline must sweep them."""
    warned = []
    monkeypatch.setattr(main, "_warn_stop_geometry",
                        lambda t, df, p: warned.append((t, df)))
    monkeypatch.setattr(main, "_load_market_context", lambda tickers, now=None: (None, None))
    monkeypatch.setattr(main, "_expected_hold_range", lambda engine: (5, 25))
    monkeypatch.setattr(main, "_load_ticker_health", lambda engine: None)
    monkeypatch.setattr(main, "_process_ticker", lambda ticker, engine, **kw: None)
    monkeypatch.setattr(main, "load_open_positions",
                        lambda: {"KO": _pos(ticker="KO"), "EFA": _pos(ticker="EFA")})

    main._run_pipeline(["KO"], SimpleNamespace(_today=None), settings={})

    # KO is scanned (checked in the per-ticker pass); EFA is not, so the sweep
    # must pick it up — with no frame, so geometry only.
    assert warned == [("EFA", None)]
