"""News cache — roundtrip, staleness, corruption quarantine, section isolation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.advisor import news_cache

_HEADS = [{"headline": "AAPL up", "source": "Reuters"}]


def test_save_and_load_roundtrip(tmp_path):
    news_cache.save_news("AAPL", "finnhub", _HEADS, cache_dir=tmp_path)
    got = news_cache.load_fresh_news("AAPL", cache_dir=tmp_path)
    assert got == _HEADS


def test_missing_file_returns_empty(tmp_path):
    assert news_cache.load_fresh_news("NOPE", cache_dir=tmp_path) == []


def test_staleness_miss(tmp_path):
    news_cache.save_news("AAPL", "finnhub", _HEADS, cache_dir=tmp_path)
    # Backdate fetched_at beyond the TTL by rewriting the file.
    path = tmp_path / "AAPL.json"
    doc = json.loads(path.read_text())
    doc["finnhub"]["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    path.write_text(json.dumps(doc))
    assert news_cache.load_fresh_news("AAPL", staleness_hours=4, cache_dir=tmp_path) == []
    assert news_cache.load_fresh_news("AAPL", staleness_hours=24, cache_dir=tmp_path) == _HEADS


def test_section_independence(tmp_path):
    news_cache.save_news("AAPL", "finnhub", [{"headline": "a"}], cache_dir=tmp_path)
    news_cache.save_news("AAPL", "search", [{"headline": "b"}], cache_dir=tmp_path)
    doc = json.loads((tmp_path / "AAPL.json").read_text())
    assert doc["finnhub"]["headlines"] == [{"headline": "a"}]
    assert doc["search"]["headlines"] == [{"headline": "b"}]
    heads = news_cache.load_fresh_news("AAPL", cache_dir=tmp_path)
    assert {"headline": "a"} in heads and {"headline": "b"} in heads


def test_corrupt_file_quarantined(tmp_path):
    path = tmp_path / "AAPL.json"
    path.write_text("{ this is not json")
    assert news_cache.load_fresh_news("AAPL", cache_dir=tmp_path) == []
    assert (tmp_path / "AAPL.json.corrupt").exists()
    assert not path.exists()


def test_atomic_write_leaves_no_tmp(tmp_path):
    news_cache.save_news("AAPL", "finnhub", _HEADS, cache_dir=tmp_path)
    assert not list(tmp_path.glob("*.tmp"))


def test_path_traversal_ticker_is_sanitized(tmp_path):
    news_cache.save_news("../evil", "finnhub", _HEADS, cache_dir=tmp_path)
    # No file escapes the cache dir.
    assert not (tmp_path.parent / "evil.json").exists()
    assert list(tmp_path.glob("*.json"))
