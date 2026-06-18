"""Wiring tests for main.py's live data-freshness guards (_ensure_fresh, _mark_review).
Pure logic with the network mocked — no real fetch/quote. See core.freshness for the calendar math.
"""
from datetime import datetime, timezone

import pandas as pd

import main
from core.filter_engine import ScanResult, SignalResult

UTC = timezone.utc


def _df(last: str, n: int = 20) -> pd.DataFrame:
    idx = pd.bdate_range(end=last, periods=n)
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000.0},
        index=idx,
    )


def test_ensure_fresh_drops_partial_current_bar():
    # Mid-session Monday: today's (6-15) bar is partial → dropped → last completed = Fri 6-12.
    df = _df("2026-06-15", n=20)
    out, behind = main._ensure_fresh("AAPL", df, datetime(2026, 6, 15, 13, 0, tzinfo=UTC))
    assert out.index[-1].date().isoformat() == "2026-06-12"
    assert behind == 0   # Friday IS the last completed session mid-Monday


def test_ensure_fresh_refetch_freshens(monkeypatch):
    stale = _df("2026-06-10", n=20)                          # ends Wed; behind by Mon post-close
    fresh = _df("2026-06-15", n=20)
    monkeypatch.setattr(main, "get_or_fetch", lambda *a, **k: fresh)
    out, behind = main._ensure_fresh("AAPL", stale, datetime(2026, 6, 15, 22, 0, tzinfo=UTC))
    assert out.index[-1].date().isoformat() == "2026-06-15" and behind == 0


def test_ensure_fresh_still_stale_when_refetch_cannot_freshen(monkeypatch):
    stale = _df("2026-06-10", n=20)
    monkeypatch.setattr(main, "get_or_fetch", lambda *a, **k: None)   # provider down / no new bar
    _out, behind = main._ensure_fresh("AAPL", stale, datetime(2026, 6, 15, 22, 0, tzinfo=UTC))
    assert behind >= 1


def _sig():
    return SignalResult(passed=True, direction="long", signal_type="momentum")


def _scan(close=100.0, atr=2.0):
    return ScanResult(passed=True, reason="ok", close=close, atr=atr)


def test_mark_review_stale_downgrades(monkeypatch):
    monkeypatch.setattr(main, "get_live_price", lambda t: 100.0)   # no gap
    s = _sig()
    main._mark_review("AAPL", s, _scan(), stale_sessions=1)
    assert s.tier == "NEEDS_REVIEW" and "stale 1 session" in s.review_reason


def test_mark_review_gap_downgrades(monkeypatch):
    monkeypatch.setattr(main, "get_live_price", lambda t: 106.0)   # gap 6 > 2×ATR(=4)
    s = _sig()
    main._mark_review("AAPL", s, _scan(close=100.0, atr=2.0), stale_sessions=0)
    assert s.tier == "NEEDS_REVIEW" and "×ATR" in s.review_reason


def test_mark_review_clean_stays_live(monkeypatch):
    monkeypatch.setattr(main, "get_live_price", lambda t: 100.5)   # gap 0.5 < 4
    s = _sig()
    main._mark_review("AAPL", s, _scan(), stale_sessions=0)
    assert s.tier == "LIVE" and s.review_reason == ""


def test_mark_review_missing_live_price_flags_unverified(monkeypatch):
    # No quote → can't verify the overnight gap. Don't fabricate a breach, but flag
    # the fire for review rather than shipping it as a clean LIVE alert (M10).
    monkeypatch.setattr(main, "get_live_price", lambda t: None)    # quote unavailable
    s = _sig()
    main._mark_review("AAPL", s, _scan(), stale_sessions=0)
    assert s.tier == "NEEDS_REVIEW" and "unverified" in s.review_reason


def test_mark_review_missing_live_price_no_atr_stays_live(monkeypatch):
    # Without a usable close/ATR there is no gap to check at all → no review flag.
    monkeypatch.setattr(main, "get_live_price", lambda t: None)
    s = _sig()
    main._mark_review("AAPL", s, _scan(close=0.0, atr=0.0), stale_sessions=0)
    assert s.tier == "LIVE" and s.review_reason == ""
