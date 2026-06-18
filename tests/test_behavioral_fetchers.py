"""
Contract tests for the behavioral / macro fetchers.

All fetchers must:
  1. Return the documented shape even when network is unreachable.
  2. Use a cache directory the caller supplies (tmp_path in tests).
  3. Never raise — failures fail-open to neutral / empty data.

These are unit-level contract tests. Live-network integration tests
should be added separately and marked ``@pytest.mark.live``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest


# ─── calendar.py ─────────────────────────────────────────────────────────────


def test_calendar_returns_events():
    from core.macro.calendar import get_calendar_events, CalendarEvent
    ev = get_calendar_events()
    assert isinstance(ev, list)
    assert len(ev) >= 36, "expected ≥ 36 events (FOMC + CPI + NFP)"
    assert all(isinstance(e, CalendarEvent) for e in ev)


def test_calendar_sorted_ascending():
    from core.macro.calendar import get_calendar_events
    ev = get_calendar_events()
    for i in range(1, len(ev)):
        assert ev[i].date >= ev[i - 1].date, "events must be sorted by date"


def test_calendar_filters_categories():
    from core.macro.calendar import get_calendar_events
    fomc = get_calendar_events(categories={"FOMC"})
    assert all(e.category == "FOMC" for e in fomc)
    assert len(fomc) >= 8, "expected ≥ 8 FOMC meetings/year"


def test_calendar_categories_case_insensitive():
    from core.macro.calendar import get_calendar_events
    upper = get_calendar_events(categories={"FOMC"})
    lower = get_calendar_events(categories={"fomc"})
    assert len(upper) == len(lower)


# ─── cot.py ──────────────────────────────────────────────────────────────────


def test_cot_unknown_contract_returns_empty():
    from core.fetchers.behavioral.cot import fetch_cot
    df = fetch_cot("bogus_contract")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_cot_known_contracts_listed():
    from core.fetchers.behavioral.cot import _COMMODITY_CODES
    assert "es" in _COMMODITY_CODES
    assert "tnote" in _COMMODITY_CODES
    assert "vix" in _COMMODITY_CODES


def test_cot_fail_open(tmp_path: Path):
    """Unknown contract or no network → empty DataFrame, never raises."""
    from core.fetchers.behavioral.cot import fetch_cot, fetch_all_cot
    df = fetch_cot("es", data_dir=tmp_path)
    assert isinstance(df, pd.DataFrame)  # may be empty if no network
    all_dfs = fetch_all_cot(data_dir=tmp_path)
    assert isinstance(all_dfs, dict)


def test_cot_normalise_handles_empty():
    from core.fetchers.behavioral.cot import _normalise_tff_rows
    assert _normalise_tff_rows([]).empty
    assert _normalise_tff_rows([{}]).empty  # no date column


def _tff_row(date: str, name: str, long_: int, short_: int) -> dict:
    return {
        "report_date_as_yyyy_mm_dd": date,
        "contract_market_name": name,
        "lev_money_positions_long_all": str(long_),
        "lev_money_positions_short_all": str(short_),
    }


def test_cot_normalise_filters_to_exact_contract():
    """The substring $where also matches MICRO E-MINI S&P 500 INDEX — those
    rows must be dropped or they interleave duplicate dates with ~10x-smaller
    positions and corrupt the positioning percentile."""
    from core.fetchers.behavioral.cot import _normalise_tff_rows
    rows = [
        _tff_row("2026-05-26", "MICRO E-MINI S&P 500 INDEX", 110_536, 126_887),
        _tff_row("2026-05-26", "E-MINI S&P 500", 149_287, 607_067),
        _tff_row("2026-05-19", "MICRO E-MINI S&P 500 INDEX", 116_779, 124_358),
        _tff_row("2026-05-19", " e-mini  s&p 500 ", 164_096, 565_650),
    ]
    df = _normalise_tff_rows(rows, "E-MINI S&P 500")
    assert len(df) == 2
    assert not df.index.duplicated().any()
    assert df["lev_net"].tolist() == [164_096 - 565_650, 149_287 - 607_067]


def test_cot_normalise_no_exact_match_keeps_all():
    """If CFTC renames the contract, fail open (keep rows, warn) rather than
    silently dropping the whole positioning axis."""
    from core.fetchers.behavioral.cot import _normalise_tff_rows
    rows = [_tff_row("2026-05-26", "E-MINI S&P 500 (RENAMED)", 1, 2)]
    df = _normalise_tff_rows(rows, "E-MINI S&P 500")
    assert len(df) == 1
    assert df["lev_net"].iloc[0] == -1


# ─── naaim.py ────────────────────────────────────────────────────────────────


def test_naaim_fail_open(tmp_path: Path):
    from core.fetchers.behavioral.naaim import fetch_naaim
    df = fetch_naaim(data_dir=tmp_path)
    assert isinstance(df, pd.DataFrame)


def test_naaim_reads_cache(tmp_path: Path):
    from core.fetchers.behavioral.naaim import fetch_naaim
    cached = pd.DataFrame(
        {"exposure": [85.0, 92.0]},
        index=pd.to_datetime(["2026-05-21", "2026-05-28"]),
    )
    cached.index.name = "date"
    parquet = tmp_path / "naaim.parquet"
    meta = tmp_path / "naaim.meta.json"
    cached.to_parquet(parquet)
    meta.write_text(json.dumps({"fetched_at": "2026-05-28T00:00:00"}))
    df = fetch_naaim(data_dir=tmp_path)
    assert not df.empty
    assert "exposure" in df.columns


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_naaim_rejects_out_of_range_scrape(monkeypatch):
    # A 3-digit number lifted off the page (e.g. an unrelated "999") must be
    # rejected, not cached as an exposure reading (audit M2).
    from core.fetchers.behavioral import naaim
    monkeypatch.setattr(naaim, "request_with_retry",
                        lambda *a, **k: _Resp("Exposure Index: 999"))
    value, _date = naaim._fetch_latest_naaim()
    assert value is None


def test_naaim_accepts_in_range_scrape(monkeypatch):
    from core.fetchers.behavioral import naaim
    monkeypatch.setattr(naaim, "request_with_retry",
                        lambda *a, **k: _Resp("Exposure Index: 87.5"))
    value, _date = naaim._fetch_latest_naaim()
    assert value == 87.5


def test_naaim_ignores_unlabelled_percentage(monkeypatch):
    # A bare "NN%" with no Exposure/NAAIM label must NOT be scraped as the value
    # (the loose fallback pattern was removed) — a failed parse returns None.
    from core.fetchers.behavioral import naaim
    monkeypatch.setattr(naaim, "request_with_retry",
                        lambda *a, **k: _Resp("Advisors are 50% bullish this week."))
    value, _date = naaim._fetch_latest_naaim()
    assert value is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
