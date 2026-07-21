"""News fetchers — source fallback chain and fail-open behavior (mocked HTTP)."""

from __future__ import annotations

import requests

from core.advisor import news_fetcher

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Apple hits record high</title><source>Reuters</source>
    <link>http://x/1</link><pubDate>Wed, 02 Jul 2026 12:00:00 +0000</pubDate></item>
  <item><title>Analysts lift AAPL target</title><source>Bloomberg</source>
    <link>http://x/2</link><pubDate>Wed, 02 Jul 2026 11:00:00 +0000</pubDate></item>
</channel></rss>"""

_TOPSTORIES = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>CPI cools more than expected</title><link>http://m/1</link></item>
  <item><title>Fed signals patience</title><link>http://m/2</link></item>
</channel></rss>"""


class _Resp:
    def __init__(self, *, text="", payload=None, exc=None):
        self.text = text
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


class _Router:
    """Fake session dispatching .get by URL substring to a response or exception."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        for frag, val in self.routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unrouted URL {url}")


def test_finnhub_returns_headlines():
    s = _Router({"finnhub.io": _Resp(payload=[
        {"headline": "AAPL beats", "source": "Finnhub", "datetime": 1, "url": "u"},
        {"headline": "", "source": "x"},  # blank headline dropped
    ])})
    out = news_fetcher.fetch_ticker_news("AAPL", finnhub_key="k", session=s)
    assert [h["headline"] for h in out] == ["AAPL beats"]
    assert not any("feeds.finance.yahoo" in c for c in s.calls)  # no fallback needed


def test_finnhub_failure_falls_back_to_yahoo():
    s = _Router({
        "finnhub.io": requests.exceptions.ConnectionError(),
        "feeds.finance.yahoo.com": _Resp(text=_RSS),
    })
    out = news_fetcher.fetch_ticker_news("AAPL", finnhub_key="k", session=s)
    assert [h["headline"] for h in out] == ["Apple hits record high", "Analysts lift AAPL target"]
    assert any("feeds.finance.yahoo" in c for c in s.calls)


def test_missing_key_uses_yahoo_directly():
    s = _Router({"feeds.finance.yahoo.com": _Resp(text=_RSS)})
    out = news_fetcher.fetch_ticker_news("AAPL", finnhub_key=None, session=s)
    assert len(out) == 2
    assert not any("finnhub" in c for c in s.calls)


def test_both_sources_fail_returns_empty():
    s = _Router({
        "finnhub.io": requests.exceptions.ConnectionError(),
        "feeds.finance.yahoo.com": requests.exceptions.Timeout(),
    })
    assert news_fetcher.fetch_ticker_news("AAPL", finnhub_key="k", session=s) == []


def test_empty_finnhub_falls_back_not_errors():
    s = _Router({
        "finnhub.io": _Resp(payload=[]),  # valid but empty
        "feeds.finance.yahoo.com": _Resp(text=_RSS),
    })
    out = news_fetcher.fetch_ticker_news("AAPL", finnhub_key="k", session=s)
    assert len(out) == 2  # empty finnhub → yahoo fallback


def test_brave_search_returns_results():
    s = _Router({"api.search.brave.com": _Resp(payload={"results": [
        {"title": "AAPL news", "url": "u", "age": "1h",
         "meta_url": {"hostname": "cnbc.com"}},
    ]})})
    out = news_fetcher.search_ticker_news("AAPL", brave_key="k", session=s)
    assert out[0]["headline"] == "AAPL news" and out[0]["source"] == "cnbc.com"


def test_brave_without_key_returns_empty():
    s = _Router({})
    assert news_fetcher.search_ticker_news("AAPL", brave_key=None, session=s) == []


def test_brave_failure_returns_empty():
    s = _Router({"api.search.brave.com": requests.exceptions.HTTPError("429")})
    assert news_fetcher.search_ticker_news("AAPL", brave_key="k", session=s) == []


def test_macro_headlines_parsed():
    s = _Router({"finance.yahoo.com/news/rssindex": _Resp(text=_TOPSTORIES)})
    out = news_fetcher.fetch_macro_headlines(session=s)
    assert [h["headline"] for h in out] == ["CPI cools more than expected", "Fed signals patience"]


def test_macro_headlines_failure_returns_empty():
    s = _Router({"finance.yahoo.com/news/rssindex": requests.exceptions.ConnectionError()})
    assert news_fetcher.fetch_macro_headlines(session=s) == []


# ── backstop engagement: catalysts, not raw relevance ────────────────────────
# Regression: the gate was `len(relevant) < 2`, which (a) counted price-recaps
# as if they were news, so two "why X moved today" pieces suppressed the keyed
# backstops entirely, and (b) left the 2..limit range unreachable, so Yahoo and
# Brave almost never ran and the model got short sets.

def _heads(*titles, source="Reuters"):
    return [{"headline": t, "source": source} for t in titles]


def _stub_sources(monkeypatch, primary):
    """Primaries return `primary`; record whether each backstop was reached."""
    hit = {"yahoo": False, "brave": False}
    monkeypatch.setattr(news_fetcher, "fetch_google_news",
                        lambda *a, **k: list(primary))
    monkeypatch.setattr(news_fetcher, "fetch_finnhub_news", lambda *a, **k: [])
    monkeypatch.setattr(news_fetcher, "fetch_alphavantage_news", lambda *a, **k: [])

    def _yahoo(ticker, limit, session):
        hit["yahoo"] = True
        return _heads("Apple signs supply agreement with Corning")

    def _brave(ticker, **k):
        hit["brave"] = True
        return _heads("Apple opens new R&D centre")

    monkeypatch.setattr(news_fetcher, "_yahoo_ticker_news", _yahoo)
    monkeypatch.setattr(news_fetcher, "search_ticker_news", _brave)
    return hit


def test_price_recaps_do_not_satisfy_the_backstop_gate(monkeypatch):
    # Two RELEVANT headlines, but both are price-recaps → no real news → the
    # backstops must still engage (old gate saw len==2 and stopped).
    recaps = _heads("Why Apple (AAPL) Stock Is Trading Up Today",
                    "Apple stock moves higher in Monday trading")
    hit = _stub_sources(monkeypatch, recaps)
    news_fetcher.gather_ticker_news("AAPL", "Apple Inc.", brave_key="k", limit=5)
    assert hit["yahoo"] and hit["brave"]


def test_backstops_engage_below_the_cap_not_just_below_two(monkeypatch):
    # Three genuine catalysts — above the old `< 2` gate but below the cap of 5,
    # the range where the keyed fallbacks were previously unreachable.
    hit = _stub_sources(monkeypatch, _heads(
        "Apple raises dividend", "Apple wins EU appeal", "Apple buys AI startup"))
    news_fetcher.gather_ticker_news("AAPL", "Apple Inc.", brave_key="k", limit=5)
    assert hit["yahoo"] and hit["brave"]


def test_backstops_skipped_once_catalysts_fill_the_cap(monkeypatch):
    hit = _stub_sources(monkeypatch, _heads(
        "Apple raises dividend", "Apple wins EU appeal", "Apple buys AI startup",
        "Apple names new CFO", "Apple expands India production"))
    out = news_fetcher.gather_ticker_news("AAPL", "Apple Inc.", brave_key="k", limit=5)
    assert not hit["yahoo"] and not hit["brave"]   # no needless keyed calls
    assert len(out) == 5


def test_brave_skipped_without_a_key_but_yahoo_still_runs(monkeypatch):
    hit = _stub_sources(monkeypatch, _heads("Apple raises dividend"))
    news_fetcher.gather_ticker_news("AAPL", "Apple Inc.", brave_key=None, limit=5)
    assert hit["yahoo"] and not hit["brave"]


# ── catalyst_count ──────────────────────────────────────────────────────────

def test_catalyst_count_excludes_price_recaps():
    items = _heads("Apple wins $2B order from TSMC",
                   "Why Apple (AAPL) Stock Is Trading Up Today")
    assert news_fetcher.catalyst_count(items) == 1
    assert news_fetcher.catalyst_count([]) == 0
