"""Unit tests for the PEAD earnings-event loader
(core.fetchers.earnings_history_store.get_earnings_events).

Pure I/O against a tmp parquet cache — no network. Verifies the loader maps
ann_date/local_hour rows to EarningsEvent with the right reaction session,
returns them sorted ascending by date, and fails open (empty list) when the
per-ticker cache file is absent.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.fetchers.earnings_history_store import get_earnings_events  # noqa: E402


def _write_cache(tmp_path: Path) -> None:
    df = pd.DataFrame([
        {"ann_date": "2020-03-02", "local_hour": 7,
         "eps_estimate": 1.0, "reported_eps": 1.1, "surprise_pct": 10.0},
        {"ann_date": "2020-01-15", "local_hour": 16,
         "eps_estimate": 2.0, "reported_eps": 1.9, "surprise_pct": -5.0},
    ])
    df.to_parquet(tmp_path / "TEST.1.parquet")


def test_get_earnings_events_sorted_with_sessions(tmp_path):
    _write_cache(tmp_path)
    events = get_earnings_events("TEST.1", cache_dir=tmp_path)

    assert len(events) == 2
    # sorted ascending by date: 2020-01-15 (AMC) first, then 2020-03-02 (BMO)
    assert [e.date.isoformat() for e in events] == ["2020-01-15", "2020-03-02"]
    assert events[0].session == "AMC"   # local_hour 16 → after close
    assert events[1].session == "BMO"   # local_hour 7 → before open


def test_get_earnings_events_missing_file_returns_empty(tmp_path):
    assert get_earnings_events("NOPE.1", cache_dir=tmp_path) == []
