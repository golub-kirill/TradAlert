"""Intraday 1h monitor — pure breakdown / completed-bar / dedup logic (no IO/network)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import intraday_monitor as im  # noqa: E402


def _pos(pid, ticker="NVDA", side="long", stop=100.0):
    return SimpleNamespace(id=pid, ticker=ticker, side=side, stop_price=stop)


def _df(closes, start="2026-07-01 10:00", freq="h"):
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    return pd.DataFrame({"close": closes}, index=idx)


# ── is_breakdown ──────────────────────────────────────────────────────────────

def test_is_breakdown():
    assert im.is_breakdown("long", 100.0, 99.5) is True
    assert im.is_breakdown("long", 100.0, 100.0) is False
    assert im.is_breakdown("long", 100.0, 101.0) is False
    assert im.is_breakdown("short", 100.0, 50.0) is False       # shorts excluded
    assert im.is_breakdown("long", None, 50.0) is False          # no stop


# ── last_completed_close ──────────────────────────────────────────────────────

def test_last_completed_close_excludes_forming_bar():
    df = _df([10, 11, 12])                                       # bars 10:00 / 11:00 / 12:00
    now = pd.Timestamp("2026-07-01 12:30")                       # 12:00 bar still forming
    iso, close = im.last_completed_close(df, now)
    assert close == 11.0 and iso.startswith("2026-07-01T11:00")


def test_last_completed_close_none_when_all_forming():
    now = pd.Timestamp("2026-07-01 10:30")
    assert im.last_completed_close(_df([10]), now) is None


def test_last_completed_close_empty_df():
    assert im.last_completed_close(pd.DataFrame(), pd.Timestamp("2026-07-01 12:00")) is None


# ── evaluate (alerts + dedup) ─────────────────────────────────────────────────

def test_evaluate_alerts_once_per_episode():
    pos = _pos(1, stop=100.0)
    alerts, state = im.evaluate([pos], {1: ("2026-07-01T11:00:00", 98.0)}, {})
    assert len(alerts) == 1 and "NVDA" in alerts[0] and "#1" in alerts[0]
    assert state == {"1": "2026-07-01T11:00:00"}
    # Next hour still broken → no re-alert (episode already flagged).
    alerts2, state2 = im.evaluate([pos], {1: ("2026-07-01T12:00:00", 97.0)}, state)
    assert alerts2 == [] and state2 == state


def test_evaluate_rearms_on_recovery():
    pos = _pos(1, stop=100.0)
    alerts, new_state = im.evaluate([pos], {1: ("2026-07-01T13:00:00", 101.0)},
                                    {"1": "2026-07-01T11:00:00"})
    assert alerts == [] and "1" not in new_state                # recovered → re-armed
    alerts2, state2 = im.evaluate([pos], {1: ("2026-07-01T14:00:00", 96.0)}, new_state)
    assert len(alerts2) == 1 and state2 == {"1": "2026-07-01T14:00:00"}


def test_evaluate_prunes_closed_positions():
    alerts, state = im.evaluate([], {}, {"9": "2026-07-01T11:00:00"})
    assert alerts == [] and state == {}


def test_evaluate_skips_positions_without_close():
    alerts, state = im.evaluate([_pos(1)], {}, {})
    assert alerts == [] and state == {}


def test_evaluate_no_alert_above_stop():
    alerts, state = im.evaluate([_pos(1, stop=100.0)], {1: ("2026-07-01T11:00:00", 105.0)}, {})
    assert alerts == [] and state == {}
