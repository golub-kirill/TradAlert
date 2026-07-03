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
