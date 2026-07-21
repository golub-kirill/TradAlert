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


# ── cache hits go through the SAME quality gate as a fresh gather ─────────────
# Regression: load_fresh_news merges the raw per-source sections verbatim, so a
# cache hit used to be returned unfiltered — wrong-company leakage, duplicates
# across sections and an uncapped list reached the model, while an identical
# cache MISS was fully filtered.

import core.advisor.service as svc  # noqa: E402
from core.advisor.news_fetcher import filter_headlines  # noqa: E402
from core.advisor.service import AdvisorContext, _resolve_headlines  # noqa: E402

# Two wrong-company items (dropped), one price-recap (kept, ordered last) and
# two catalysts — enough real news to clear MIN_CATALYSTS so the cache is served
# rather than refetched.
_MIXED = [
    {"headline": "ASML Q2 Earnings Loom: Buy, Sell or Hold?", "source": "Yahoo"},
    {"headline": "Analysts Raised AMD Price Target Again", "source": "Yahoo"},
    {"headline": "Why Applied Materials (AMAT) Stock Is Trading Up Today", "source": "Yahoo"},
    {"headline": "Applied Materials wins $2B order from TSMC", "source": "Reuters"},
    {"headline": "Applied Materials raises full-year guidance", "source": "Reuters"},
]


def _ctx(**over):
    kw = dict(enabled=True, read_only=False, cache_ttl_hours=4.0,
              max_headlines=5, sec_filings=False)
    kw.update(over)
    return AdvisorContext(**kw)


def _point_cache_at(monkeypatch, tmp_path):
    monkeypatch.setattr(
        svc, "load_fresh_news",
        lambda t, staleness_hours=4.0: news_cache.load_fresh_news(
            t, staleness_hours=staleness_hours, cache_dir=tmp_path))


def test_cache_hit_drops_wrong_company_and_dedupes(monkeypatch, tmp_path):
    # Same payload in two sections → load_fresh_news returns 10 raw items.
    news_cache.save_news("AMAT", "finnhub", _MIXED, cache_dir=tmp_path)
    news_cache.save_news("AMAT", "gathered", _MIXED, cache_dir=tmp_path)
    _point_cache_at(monkeypatch, tmp_path)

    out = _resolve_headlines("AMAT", _ctx(), "Applied Materials")
    heads = [h["headline"] for h in out]

    assert len(heads) == 3                       # was 10 raw / 6 unfiltered
    assert not any("ASML" in h or "AMD" in h for h in heads)   # wrong company
    assert len(heads) == len(set(heads))         # cross-section duplicates gone


def test_cache_hit_puts_catalysts_before_price_recaps(monkeypatch, tmp_path):
    news_cache.save_news("AMAT", "finnhub", _MIXED, cache_dir=tmp_path)
    _point_cache_at(monkeypatch, tmp_path)
    out = _resolve_headlines("AMAT", _ctx(), "Applied Materials")
    heads = [h["headline"] for h in out]
    assert "Trading Up Today" in heads[-1]       # the price-recap sinks to last
    assert all("Trading Up Today" not in h for h in heads[:-1])


def test_cache_hit_respects_the_headline_cap(monkeypatch, tmp_path):
    many = [{"headline": f"Applied Materials announces deal number {i}",
             "source": "Reuters"} for i in range(12)]
    news_cache.save_news("AMAT", "finnhub", many, cache_dir=tmp_path)
    _point_cache_at(monkeypatch, tmp_path)
    out = _resolve_headlines("AMAT", _ctx(max_headlines=5), "Applied Materials")
    assert len(out) == 5


def test_thin_cache_falls_through_to_a_fresh_gather(monkeypatch, tmp_path):
    # Only ONE catalyst survives → below MIN_CATALYSTS → refetch (which also
    # engages the keyed backstops) instead of serving a stub.
    news_cache.save_news("AMAT", "finnhub", [
        {"headline": "ASML Q2 Earnings Loom", "source": "Yahoo"},
        {"headline": "Applied Materials wins $2B order from TSMC", "source": "Reuters"},
    ], cache_dir=tmp_path)
    _point_cache_at(monkeypatch, tmp_path)

    called = {}

    def _fake_gather(ticker, company_name="", **kw):
        called["hit"] = True
        return [{"headline": "Applied Materials raises guidance", "source": "Reuters"}]

    monkeypatch.setattr(svc, "gather_ticker_news", _fake_gather)
    out = _resolve_headlines("AMAT", _ctx(), "Applied Materials")
    assert called.get("hit") is True
    assert out[0]["headline"] == "Applied Materials raises guidance"


def test_filter_headlines_is_the_shared_gate():
    kept = filter_headlines(_MIXED, "AMAT", "Applied Materials", limit=5)
    heads = [h["headline"] for h in kept]
    assert len(heads) == 3 and "Trading Up Today" in heads[-1]
    # Same call with a tight cap truncates after the catalyst-first sort.
    assert len(filter_headlines(_MIXED, "AMAT", "Applied Materials", limit=1)) == 1


def test_cache_of_only_price_recaps_is_refetched(monkeypatch, tmp_path):
    # Relevant but information-free: two price-recaps cleared the old count-based
    # gate and were served. They carry no catalyst, so the cache must be refetched.
    news_cache.save_news("AMAT", "finnhub", [
        {"headline": "Why Applied Materials (AMAT) Stock Is Trading Up Today", "source": "Yahoo"},
        {"headline": "Applied Materials stock moves higher Monday", "source": "Yahoo"},
    ], cache_dir=tmp_path)
    _point_cache_at(monkeypatch, tmp_path)

    called = {}

    def _fake_gather(ticker, company_name="", **kw):
        called["hit"] = True
        return [{"headline": "Applied Materials wins $2B TSMC order", "source": "Reuters"}]

    monkeypatch.setattr(svc, "gather_ticker_news", _fake_gather)
    monkeypatch.setattr(svc, "save_news", lambda *a, **k: None)
    out = _resolve_headlines("AMAT", _ctx(), "Applied Materials")
    assert called.get("hit") is True
    assert "TSMC" in out[0]["headline"]
