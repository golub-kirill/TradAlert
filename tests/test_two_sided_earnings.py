"""Two-sided earnings buffer (``events.earnings_buffer_two_sided``, opt-in).

Default OFF → byte-identical baseline: ``_recent_earnings`` returns False no
matter what date it is handed, and the threaded ``prev_earnings_date`` kwarg is
inert. ON → an entry inside the buffer AFTER the last earnings blocks with a
named reason, symmetric to the forward arm.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from core.filter_engine import FilterEngine

_TODAY = date(2025, 6, 15)


def _engine(two_sided: bool) -> FilterEngine:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg.setdefault("signals", {})
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["signals"]["require_trigger_bar_up"] = False
    cfg["events"] = {"earnings_buffer_days": 5, "stop_dates": [],
                     "earnings_buffer_two_sided": two_sided}
    eng = FilterEngine.from_dict(cfg, today=_TODAY)
    eng._evaluate_entry = lambda *a, **k: ("long", "momentum", "stub")
    return eng


def _firing_df(n_warmup: int = 260) -> pd.DataFrame:
    def row(mh):
        return dict(open=99.0, high=102.0, low=98.0, close=101.0, volume=1_000_000,
                    atr=1.0, rsi=55.0, macd=0.1, macd_signal=0.05, macd_hist=mh,
                    bb_bw=3.0, bb_z=0.4, weekly_sma10=96.0,
                    ma_fast=95.5, ma_slow=90.5)
    rows = [row(-0.05) for _ in range(n_warmup)] + [row(0.05), row(0.10)]
    return pd.DataFrame(rows, index=pd.date_range("2023-01-01", periods=len(rows), freq="B"))


# ── helper semantics ──────────────────────────────────────────────────────────

def test_recent_earnings_off_by_default():
    eng = _engine(two_sided=False)
    assert eng.cfg.events.earnings_buffer_two_sided is False
    assert eng._recent_earnings(_TODAY - timedelta(days=1)) is False


def test_recent_earnings_window_when_enabled():
    eng = _engine(two_sided=True)
    assert eng._recent_earnings(_TODAY - timedelta(days=1)) is True
    assert eng._recent_earnings(_TODAY - timedelta(days=5)) is True   # edge of buffer
    assert eng._recent_earnings(_TODAY - timedelta(days=6)) is False  # outside
    assert eng._recent_earnings(_TODAY) is True                       # earnings today
    assert eng._recent_earnings(None) is False
    assert eng._recent_earnings(_TODAY + timedelta(days=2)) is False  # forward arm's job


# ── gate integration ──────────────────────────────────────────────────────────

def test_two_sided_blocks_entry_with_named_reason():
    eng = _engine(two_sided=True)
    r = eng.signal("ABC", _firing_df(),
                   prev_earnings_date=_TODAY - timedelta(days=2))
    assert r.passed is False
    assert "two-sided buffer" in r.reason and "2d ago" in r.reason


def test_default_off_ignores_prev_earnings_kwarg():
    eng = _engine(two_sided=False)
    r = eng.signal("ABC", _firing_df(),
                   prev_earnings_date=_TODAY - timedelta(days=2))
    assert r.passed is True   # stubbed entry fires; the kwarg is inert


def test_enabled_passes_outside_the_window():
    eng = _engine(two_sided=True)
    r = eng.signal("ABC", _firing_df(),
                   prev_earnings_date=_TODAY - timedelta(days=30))
    assert r.passed is True


# ── backtester feed helper ────────────────────────────────────────────────────

def test_prev_earnings_from_picks_latest_strictly_past():
    from backtest.backtester import _prev_earnings_from
    h = [date(2025, 1, 2), date(2025, 4, 2), date(2025, 7, 1)]
    assert _prev_earnings_from(h, date(2025, 6, 15)) == date(2025, 4, 2)
    assert _prev_earnings_from(h, date(2025, 4, 2)) == date(2025, 1, 2)  # strict
    assert _prev_earnings_from(h, date(2025, 1, 1)) is None
    assert _prev_earnings_from([], date(2025, 1, 1)) is None
