"""Unit tests for the TradingView economic-calendar fetcher (core.fetchers.macro.tv_calendar).

Pure / mocked — no network. Guards the parser's classification + importance rules
(FOMC is importance-exempt by exact title; CPI/NFP require importance>=1) and the
fail-open contract.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.fetchers.macro import tv_calendar as tv  # noqa: E402

_SAMPLE = [
    {"title": "Fed Interest Rate Decision", "importance": 0, "date": "2018-03-21T18:00:00Z"},  # FOMC (imp-0 ok)
    {"title": "FOMC Minutes", "importance": 0, "date": "2018-04-11T18:00:00Z"},                # excluded (not decision)
    {"title": "FOMC Economic Projections", "importance": -1, "date": "2018-03-21T18:00:00Z"},  # excluded
    {"title": "Fed Kashkari Speech", "importance": 0, "date": "2018-03-22T13:00:00Z"},         # excluded (speech)
    {"title": "Inflation Rate YoY", "importance": 1, "date": "2018-03-13T12:30:00Z"},          # CPI
    {"title": "Core Inflation Rate YoY", "importance": 1, "date": "2018-03-13T12:30:00Z"},     # CPI dup-day
    {"title": "Inflation Rate YoY", "importance": 0, "date": "2017-01-13T12:30:00Z"},          # excluded (imp<1)
    {"title": "Non Farm Payrolls", "importance": 1, "date": "2018-03-09T12:30:00Z"},           # NFP
    {"title": "ISM Manufacturing PMI", "importance": 1, "date": "2018-03-01T14:00:00Z"},       # excluded (not big-3)
]


def test_parse_classifies_and_filters():
    df = tv._parse(_SAMPLE)
    got = {(str(r.date.date()), r.category) for r in df.itertuples()}
    assert ("2018-03-21", "FOMC") in got     # importance-0 FOMC kept (exact title)
    assert ("2018-03-13", "CPI") in got      # CPI kept
    assert ("2018-03-09", "NFP") in got      # NFP kept
    # exclusions
    cats_titles = set(df["category"])
    assert all(c in {"FOMC", "CPI", "NFP"} for c in cats_titles)
    assert not ((df["category"] == "CPI") & (df["date"].dt.year == 2017)).any()  # imp-0 CPI dropped
    # FOMC Minutes / Projections / speech / ISM all absent
    assert len(df) == 3   # one FOMC + one CPI (deduped YoY+Core) + one NFP


def test_parse_dedupes_same_day_category():
    rows = [
        {"title": "Inflation Rate YoY", "importance": 1, "date": "2020-06-10T12:30:00Z"},
        {"title": "Inflation Rate MoM", "importance": 1, "date": "2020-06-10T12:30:00Z"},
        {"title": "Core Inflation Rate YoY", "importance": 1, "date": "2020-06-10T12:30:00Z"},
    ]
    df = tv._parse(rows)
    assert len(df) == 1 and df.iloc[0]["category"] == "CPI"


def test_parse_empty():
    df = tv._parse([])
    assert df.empty and list(df.columns) == ["date", "category", "title"]


def test_fetch_fail_open_returns_empty(monkeypatch, tmp_path):
    """Network failure with no cache → empty frame, never raises."""
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(tv, "_fetch_window", _boom)
    df = tv.fetch_tv_calendar("2024-01-01", "2024-03-31",
                              countries=("US",), cache_dir=tmp_path, force=True)
    assert df.empty


def test_classify():
    assert tv._classify("Fed Interest Rate Decision") == "FOMC"
    assert tv._classify("FOMC Minutes") is None
    assert tv._classify("Non Farm Payrolls") == "NFP"
    assert tv._classify("Core Inflation Rate MoM") == "CPI"
    assert tv._classify("ISM Manufacturing PMI") is None
