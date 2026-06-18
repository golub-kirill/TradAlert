"""LIVE-only context freshness (audit M1 + H3 part 2), both in main.py:

  • _load_market_context drops any unclosed current-day bar from each regime
    context frame (SPY/QQQ/^VIX) so the regime is never classified on a partial bar;
  • _drop_stale_behavioral removes behavioral feeds whose data-date is past the
    staleness window, so the live classifier treats them as missing (confidence
    falls) instead of sizing on month-old data.

Both are live-only; the backtester slices these feeds point-in-time and never
calls either function.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import main


def _frame():
    idx = pd.bdate_range("2026-06-01", periods=5)
    return pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]}, index=idx)


# ── M1: drop_unclosed_bar on each context frame ──────────────────────────────

def test_load_market_context_drops_unclosed_bar_on_each_frame(monkeypatch):
    frames = {"SPY": _frame(), "QQQ": _frame(), "^VIX": _frame()}
    monkeypatch.setattr(main, "cache_load", lambda s: frames[s])

    seen = []

    def _fake_drop(df, now, exch):
        seen.append((len(df), now, exch))
        return df.iloc[:-1]  # stand in for trimming the unclosed bar

    monkeypatch.setattr(main, "drop_unclosed_bar", _fake_drop)
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)

    market_dfs, vix_df = main._load_market_context(["SPY", "QQQ", "^VIX"], now=now)

    # SPY, QQQ and ^VIX each went through the trim with the injected `now`.
    assert len(seen) == 3
    assert all(s[1] is now for s in seen)
    assert {s[2] for s in seen} == {"NYSE"}
    assert len(market_dfs["SPY"]) == 4 and len(market_dfs["QQQ"]) == 4
    assert vix_df is not None and len(vix_df) == 4


# ── H3 part 2: staleness pre-filter ──────────────────────────────────────────

_NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)


def _feed(last_date: str):
    return pd.DataFrame({"v": [1.0]}, index=pd.DatetimeIndex([pd.Timestamp(last_date)]))


def test_drop_stale_behavioral_removes_old_feeds_keeps_fresh(caplog):
    data = {"breadth": _feed("2026-06-15"), "cot_es": _feed("2026-04-01")}
    with caplog.at_level("WARNING"):
        out = main._drop_stale_behavioral(data, _NOW, stale_days=14)
    assert set(out) == {"breadth"}            # the month-old feed is dropped
    assert "cot_es" in caplog.text and "STALE" in caplog.text


def test_drop_stale_behavioral_noop_when_window_nonpositive():
    data = {"x": _feed("2000-01-01")}
    assert main._drop_stale_behavioral(data, _NOW, stale_days=0) is data


def test_drop_stale_behavioral_keeps_unjudgeable_feeds():
    # An empty frame or a non-datetime index can't be aged → keep it (let the
    # classifier handle absence), never wrongly drop.
    data = {"empty": pd.DataFrame(), "rangeidx": pd.DataFrame({"v": [1, 2]})}
    out = main._drop_stale_behavioral(data, _NOW, stale_days=14)
    assert set(out) == {"empty", "rangeidx"}
