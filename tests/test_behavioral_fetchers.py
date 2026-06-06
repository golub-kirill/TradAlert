"""
Contract tests for the six behavioral / macro fetchers.

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


# ─── short_interest.py ───────────────────────────────────────────────────────


def test_short_interest_fail_open(tmp_path: Path):
    from core.fetchers.behavioral.short_interest import fetch_short_interest
    out = fetch_short_interest("XYZ_BOGUS", data_dir=tmp_path)
    assert isinstance(out, dict)
    assert "short_percent_of_float" in out
    # Either None (fetch failed) or a float (cached unexpectedly) — both OK.
    val = out["short_percent_of_float"]
    assert val is None or isinstance(val, (int, float))


def test_short_interest_reads_cache(tmp_path: Path):
    """Pre-populate cache; fetch should return it without network."""
    from core.fetchers.behavioral.short_interest import fetch_short_interest
    cache_path = tmp_path / "AAPL.json"
    cache_path.write_text(json.dumps({
        "short_percent_of_float": 0.043,
        "fetched_at": "2026-05-27T00:00:00",
    }))
    out = fetch_short_interest("AAPL", data_dir=tmp_path)
    assert out["short_percent_of_float"] == 0.043


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
    # cot.py moved Disaggregated → TFF; the normaliser is now _normalise_tff_rows.
    from core.fetchers.behavioral.cot import _normalise_tff_rows
    assert _normalise_tff_rows([]).empty
    assert _normalise_tff_rows([{}]).empty  # no date column


# ─── form4.py ────────────────────────────────────────────────────────────────


def test_form4_fail_open_shape(tmp_path: Path):
    from core.fetchers.behavioral.form4 import fetch_form4, _ZERO
    out = fetch_form4("XYZ_BOGUS", data_dir=tmp_path)
    assert isinstance(out, dict)
    # All required scoring keys present.
    for k in _ZERO:
        assert k in out, f"missing required key {k}"
    # Types match scoring expectations.
    assert isinstance(out["buys_30d"], int)
    assert isinstance(out["buys_90d"], int)
    assert isinstance(out["sells_90d"], int)
    assert isinstance(out["buy_value_30d"], (int, float))
    assert isinstance(out["sell_value_90d"], (int, float))
    assert isinstance(out["cluster_buy_30d"], bool)


def test_form4_zero_dict_complete():
    """The _ZERO fallback must satisfy SignalScorer._score_insider_buying."""
    from core.fetchers.behavioral.form4 import _ZERO
    required = {
        "buys_30d", "buys_90d", "sells_90d",
        "buy_value_30d", "sell_value_90d",
        "distinct_insiders_30d", "cluster_buy_30d",
    }
    assert required.issubset(_ZERO.keys())


def test_form4_summarise_empty():
    """Empty transactions DataFrame → zero-filled summary."""
    from core.fetchers.behavioral.form4 import _summarise_transactions, _ZERO
    df = pd.DataFrame(columns=["Start Date", "Transaction"])
    out = _summarise_transactions(df)
    for k in _ZERO:
        assert out[k] == _ZERO[k]


# ─── aaii.py ─────────────────────────────────────────────────────────────────


def test_aaii_fail_open(tmp_path: Path):
    from core.fetchers.behavioral.aaii import fetch_aaii
    df = fetch_aaii(data_dir=tmp_path)
    # No network in sandbox → empty DataFrame, never raises.
    assert isinstance(df, pd.DataFrame)


def test_aaii_reads_cache(tmp_path: Path):
    """Pre-populate parquet + meta; fetch returns cached data."""
    from core.fetchers.behavioral.aaii import fetch_aaii
    # Build a minimal cache.
    cached = pd.DataFrame(
        {"bullish": [0.4, 0.42], "bearish": [0.3, 0.28], "spread": [0.1, 0.14]},
        index=pd.to_datetime(["2026-05-20", "2026-05-27"]),
    )
    cached.index.name = "date"
    parquet = tmp_path / "aaii.parquet"
    meta = tmp_path / "aaii.meta.json"
    cached.to_parquet(parquet)
    meta.write_text(json.dumps({"fetched_at": "2026-05-27T00:00:00"}))
    df = fetch_aaii(data_dir=tmp_path)
    assert not df.empty
    assert "spread" in df.columns


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
