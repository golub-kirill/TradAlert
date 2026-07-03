"""News fetchers for the advisor — all fail-open, all return ``[]`` on error.

Source chain for ticker news:
    Finnhub (JSON, keyed)  →  Yahoo Finance RSS (keyless)  →  Brave (keyed, fallback)

Macro context uses Yahoo Finance top-stories RSS (keyless). No fetcher ever
raises; the caller checks ``if headlines:`` before using the result.

Headlines are normalized to ``{"headline", "source", "datetime", "url"}`` dicts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

try:
    from core.fetchers.yf_fetchOne import to_yf_symbol
except Exception:  # pragma: no cover - symbology mapping is best-effort
    def to_yf_symbol(t: str) -> str:  # type: ignore
        return t

logger = logging.getLogger(__name__)

__all__ = ["fetch_ticker_news", "search_ticker_news", "fetch_macro_headlines"]

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TradAlert/1.0)"}
_FINNHUB_NEWS = "https://finnhub.io/api/v1/company-news"
_YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
_YAHOO_TOPSTORIES_RSS = "https://finance.yahoo.com/news/rssindex"
_BRAVE_NEWS = "https://api.search.brave.com/res/v1/news/search"


def _parse_rss(xml_text: str, limit: int) -> list[dict]:
    """Parse an RSS 2.0 feed into normalized headline dicts (lxml XML parser)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(xml_text, "xml")
    out: list[dict] = []
    for item in soup.find_all("item")[:limit]:
        title = item.title.get_text(strip=True) if item.title else ""
        if not title:
            continue
        src_tag = item.find("source")
        source = src_tag.get_text(strip=True) if src_tag else "Yahoo Finance"
        link = item.link.get_text(strip=True) if item.link else ""
        pub = item.find("pubDate")
        out.append({
            "headline": title,
            "source": source,
            "datetime": pub.get_text(strip=True) if pub else "",
            "url": link,
        })
    return out


def _yahoo_ticker_news(ticker: str, limit: int, session: requests.Session | None) -> list[dict]:
    try:
        params = {"s": to_yf_symbol(ticker), "region": "US", "lang": "en-US"}
        get = (session or requests).get
        resp = get(_YAHOO_TICKER_RSS, params=params, headers=_UA, timeout=10)
        resp.raise_for_status()
        return _parse_rss(resp.text, limit)
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("news_fetcher yahoo ticker RSS failed for %s — skipped: %s", ticker, exc)
        return []


def fetch_ticker_news(
        ticker: str,
        *,
        finnhub_key: str | None = None,
        session: requests.Session | None = None,
        limit: int = 5,
        lookback_days: int = 7,
) -> list[dict]:
    """Ticker headlines: Finnhub primary, Yahoo RSS fallback. ``[]`` on failure."""
    if finnhub_key:
        try:
            today = datetime.now(timezone.utc).date()
            params = {
                "symbol": ticker.upper(),
                "from": (today - timedelta(days=lookback_days)).isoformat(),
                "to": today.isoformat(),
                "token": finnhub_key,
            }
            get = (session or requests).get
            resp = get(_FINNHUB_NEWS, params=params, headers=_UA, timeout=10)
            resp.raise_for_status()
            items = resp.json()
            if isinstance(items, list) and items:
                out: list[dict] = []
                for it in items[:limit]:
                    head = str(it.get("headline") or "").strip()
                    if not head:
                        continue
                    out.append({
                        "headline": head,
                        "source": str(it.get("source") or "Finnhub"),
                        "datetime": it.get("datetime", ""),
                        "url": str(it.get("url") or ""),
                    })
                if out:
                    return out
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            logger.warning("news_fetcher finnhub failed for %s — falling back: %s", ticker, exc)

    return _yahoo_ticker_news(ticker, limit, session)


def search_ticker_news(
        ticker: str,
        *,
        brave_key: str | None = None,
        session: requests.Session | None = None,
        limit: int = 5,
) -> list[dict]:
    """Brave News search fallback (cache-miss path). ``[]`` on failure/no key."""
    if not brave_key:
        return []
    try:
        headers = {**_UA, "X-Subscription-Token": brave_key, "Accept": "application/json"}
        params = {"q": f"{ticker} stock news", "count": limit}
        get = (session or requests).get
        resp = get(_BRAVE_NEWS, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
        out: list[dict] = []
        for r in results[:limit]:
            head = str(r.get("title") or "").strip()
            if not head:
                continue
            out.append({
                "headline": head,
                "source": str((r.get("meta_url") or {}).get("hostname") or "Brave"),
                "datetime": str(r.get("age") or ""),
                "url": str(r.get("url") or ""),
            })
        return out
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("news_fetcher brave search failed for %s — skipped: %s", ticker, exc)
        return []


def fetch_macro_headlines(
        *,
        session: requests.Session | None = None,
        limit: int = 12,
) -> list[dict]:
    """Yahoo Finance top-stories RSS for market-wide context. ``[]`` on failure."""
    try:
        get = (session or requests).get
        resp = get(_YAHOO_TOPSTORIES_RSS, headers=_UA, timeout=10)
        resp.raise_for_status()
        return _parse_rss(resp.text, limit)
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("news_fetcher yahoo topstories failed — skipped: %s", exc)
        return []
