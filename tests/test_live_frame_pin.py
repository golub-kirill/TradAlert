"""Live decision-frame pin + expired-blackout probe.

The backtest pins ``engine._today`` to the bar (backtester.py, asserted by
test_no_lookahead); the live scan historically ran on the wall clock, so the two
paths screened different populations around earnings. ``main._frame_date`` is
the live-side anchor: the last completed regime-index bar, applied to
``engine._today`` in ``_run_pipeline``. ``stop_dates_dark`` surfaces an
events.stop_dates list that has entirely expired.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yaml

import main
from core.filter_engine import FilterEngine


def _cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ── _frame_date ───────────────────────────────────────────────────────────────

def test_frame_date_none_without_context():
    assert main._frame_date(None) is None
    assert main._frame_date({}) is None


def test_frame_date_is_last_completed_bar():
    df = pd.DataFrame({"close": [1.0, 2.0]},
                      index=pd.to_datetime(["2026-07-14", "2026-07-15"]))
    assert main._frame_date({"SPY": df}) == date(2026, 7, 15)


def test_frame_date_takes_max_across_frames():
    """One stale index frame must not drag the scan's frame backwards."""
    fresh = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2026-07-15"]))
    stale = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2026-07-14"]))
    assert main._frame_date({"SPY": fresh, "QQQ": stale}) == date(2026, 7, 15)


def test_frame_date_skips_empty_frames():
    empty = pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))
    fresh = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2026-07-15"]))
    assert main._frame_date({"SPY": empty, "QQQ": fresh}) == date(2026, 7, 15)
    assert main._frame_date({"SPY": empty}) is None


# ── stop_dates_dark ───────────────────────────────────────────────────────────

def test_stop_dates_dark_when_every_row_is_past():
    cfg = _cfg()
    rows = (cfg.get("events") or {}).get("stop_dates") or []
    assert rows, "shipped config carries stop_dates rows; fixture assumption"
    latest = max(date.fromisoformat(str(r["date"])) for r in rows)
    eng = FilterEngine.from_dict(cfg, today=latest.replace(year=latest.year + 1))
    assert eng.stop_dates_dark() == latest


def test_stop_dates_not_dark_while_a_row_is_current():
    cfg = _cfg()
    rows = (cfg.get("events") or {}).get("stop_dates") or []
    earliest = min(date.fromisoformat(str(r["date"])) for r in rows)
    eng = FilterEngine.from_dict(cfg, today=earliest)
    assert eng.stop_dates_dark() is None


def test_stop_dates_dark_none_when_unconfigured():
    cfg = _cfg()
    cfg["events"]["stop_dates"] = []
    eng = FilterEngine.from_dict(cfg, today=date(2030, 1, 1))
    assert eng.stop_dates_dark() is None
