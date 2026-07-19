"""News fetchers for the advisor — all fail-open, all return ``[]`` on error.

Source chain for ticker news:
    Finnhub (JSON, keyed)  →  Yahoo Finance RSS (keyless)  →  Brave (keyed, fallback)

Macro context uses Yahoo Finance top-stories RSS (keyless). No fetcher ever
raises; the caller checks ``if headlines:`` before using the result.

Headlines are normalized to ``{"headline", "source", "datetime", "url"}`` dicts.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

from core.advisor.news_query import (
    build_queries,
    company_aliases,
    is_noise,
    is_price_recap,
    is_relevant,
    symbol_root,
)

try:
    from core.fetchers.yf_fetchOne import to_yf_symbol
except Exception:  # pragma: no cover - symbology mapping is best-effort
    def to_yf_symbol(t: str) -> str:  # type: ignore
        return t

logger = logging.getLogger(__name__)

__all__ = ["fetch_ticker_news", "search_ticker_news", "fetch_macro_headlines",
           "fetch_finnhub_news", "fetch_alphavantage_news", "fetch_google_news",
           "gather_ticker_news", "fetch_sec_filings"]

# SEC EDGAR — free, no key, requires a declared User-Agent. 8-K = material events.
_SEC_UA = {"User-Agent": "TradAlert/1.0 research (admin@tradealert.local)"}
_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# 8-K item codes → the material event they signal (the high-signal subset).
_8K_ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated",
    "1.03": "bankruptcy/receivership",
    "2.01": "acquisition/disposition", "2.02": "earnings results",
    "2.03": "material obligation", "2.04": "triggering event on obligation",
    "2.05": "restructuring costs", "2.06": "material impairment",
    "3.01": "delisting notice", "3.03": "shareholder-rights change",
    "4.01": "auditor change", "4.02": "financials no longer reliable",
    "5.01": "change in control", "5.02": "exec/director change",
    "5.03": "bylaw change", "5.07": "shareholder vote results",
    "7.01": "Reg-FD disclosure",
    "8.01": "other material event", "9.01": "financial exhibits",
}
_cik_map: dict | None = None  # ticker(upper) → CIK int; lazy-loaded once

_UA = {"User-Agent": "Mozilla/5.0 (compatible; TradAlert/1.0)"}
_FINNHUB_NEWS = "https://finnhub.io/api/v1/company-news"
_ALPHAVANTAGE = "https://www.alphavantage.co/query"
_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
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


def fetch_finnhub_news(
        symbol: str,
        key: str,
        *,
        session: requests.Session | None = None,
        limit: int = 5,
        lookback_days: int = 7,
) -> list[dict]:
    """Finnhub company-news for a base symbol. ``[]`` on failure. Returns more
    than ``limit`` so the caller can relevance-filter then cap (Finnhub mixes in
    market-wide items for large caps)."""
    if not key:
        return []
    try:
        today = datetime.now(timezone.utc).date()
        params = {
            "symbol": symbol.upper(),
            "from": (today - timedelta(days=lookback_days)).isoformat(),
            "to": today.isoformat(),
            "token": key,
        }
        get = (session or requests).get
        resp = get(_FINNHUB_NEWS, params=params, headers=_UA, timeout=10)
        resp.raise_for_status()
        items = resp.json()
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("news_fetcher finnhub failed for %s — skipped: %s", symbol, exc)
        return []
    out: list[dict] = []
    for it in (items if isinstance(items, list) else [])[: limit * 3]:
        head = str(it.get("headline") or "").strip()
        if head:
            out.append({"headline": head, "source": str(it.get("source") or "Finnhub"),
                        "datetime": it.get("datetime", ""), "url": str(it.get("url") or "")})
    return out


def fetch_alphavantage_news(
        symbol: str,
        key: str,
        *,
        session: requests.Session | None = None,
        limit: int = 5,
) -> list[dict]:
    """AlphaVantage NEWS_SENTIMENT for a base symbol — carries per-ticker
    relevance + sentiment. ``[]`` on failure/rate-limit. Free tier is 25/day, so
    the caller budgets calls before invoking this."""
    if not key or not symbol.isalpha():  # AV rejects dotted/hyphenated symbols
        return []
    try:
        params = {"function": "NEWS_SENTIMENT", "tickers": symbol.upper(),
                  "limit": max(limit * 4, 20), "apikey": key}
        get = (session or requests).get
        resp = get(_ALPHAVANTAGE, params=params, headers=_UA, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("news_fetcher alphavantage failed for %s — skipped: %s", symbol, exc)
        return []
    feed = data.get("feed") if isinstance(data, dict) else None
    if not feed:  # {"Note": ...} rate-limit or {"Error Message": ...}
        note = (data.get("Note") or data.get("Information") or data.get("Error Message")
                if isinstance(data, dict) else "")
        if note:
            logger.warning("news_fetcher alphavantage no feed for %s: %s", symbol, str(note)[:120])
        return []
    out: list[dict] = []
    for a in feed:
        head = str(a.get("title") or "").strip()
        if not head:
            continue
        ts = [t for t in (a.get("ticker_sentiment") or [])
              if str(t.get("ticker", "")).upper() == symbol.upper()]
        item = {"headline": head, "source": str(a.get("source") or "AlphaVantage"),
                "datetime": str(a.get("time_published") or ""), "url": str(a.get("url") or "")}
        if ts:
            try:
                item["relevance"] = float(ts[0].get("relevance_score"))
                item["sentiment"] = float(ts[0].get("ticker_sentiment_score"))
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def fetch_google_news(
        query: str,
        *,
        session: requests.Session | None = None,
        limit: int = 5,
) -> list[dict]:
    """Google News RSS for a free-text (company-name) query — keyless, universal,
    query-driven so it returns the RIGHT company for foreign listings. ``[]`` on
    failure."""
    if not query:
        return []
    try:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        get = (session or requests).get
        resp = get(_GOOGLE_NEWS_RSS, params=params, headers=_UA, timeout=10)
        resp.raise_for_status()
        items = _parse_rss(resp.text, limit * 3)
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("news_fetcher google news failed for %r — skipped: %s", query, exc)
        return []
    # Google titles are "Headline - Publisher"; the publisher is also in <source>,
    # so strip the redundant trailing " - Publisher" for cleaner text + dedup.
    for it in items:
        src = it.get("source") or ""
        if src and it["headline"].endswith(f" - {src}"):
            it["headline"] = it["headline"][: -(len(src) + 3)].rstrip()
    return items


def _av_budget_ok(max_per_day: int) -> bool:
    """True (and record a call) if today's AlphaVantage budget isn't spent.

    Persists ``{date, count}`` in the news cache dir so the 25/day free cap is
    honored across scans. Fail-open: any IO error just allows the call."""
    if max_per_day <= 0:
        return False
    from core.paths import NEWS_DIR
    path = NEWS_DIR / ".av_budget.json"
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    count = int(doc.get("count", 0)) if doc.get("date") == today else 0
    if count >= max_per_day:
        return False
    try:
        NEWS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": today, "count": count + 1}), encoding="utf-8")
    except OSError:
        pass
    return True


def _load_cik_map(session: requests.Session | None) -> dict:
    """Ticker→CIK map from SEC's company_tickers.json, cached for the process.
    ``{}`` on failure — SEC filings simply won't augment the news then."""
    global _cik_map
    if _cik_map is not None:
        return _cik_map
    try:
        get = (session or requests).get
        resp = get(_SEC_TICKERS, headers=_SEC_UA, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}
        _cik_map = {str(v["ticker"]).upper(): int(v["cik_str"])
                    for v in data.values() if v.get("ticker")}
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("news_fetcher SEC cik map failed — skipped: %s", exc)
        _cik_map = {}
    return _cik_map


def fetch_sec_filings(
        ticker: str,
        *,
        session: requests.Session | None = None,
        limit: int = 2,
        lookback_days: int = 21,
) -> list[dict]:
    """Recent 8-K **material-event** filings for a US ticker — the highest-signal
    ticker-specific news (earnings, M&A, guidance, exec changes, impairments).

    ``[]`` for non-US / unmapped tickers (no CIK) and on any failure. Requires no
    API key. Normalized to the same headline dict shape as the news fetchers."""
    cik = _load_cik_map(session).get(ticker.upper())
    if cik is None:
        return []  # non-US or unmapped — nothing to add
    try:
        get = (session or requests).get
        resp = get(_SEC_SUBMISSIONS.format(cik=cik), headers=_SEC_UA, timeout=10)
        resp.raise_for_status()
        recent = ((resp.json() or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items = recent.get("items") or []
        accns = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
        out: list[dict] = []
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            try:
                fdate = datetime.strptime(dates[i], "%Y-%m-%d").date()
            except (ValueError, IndexError):
                continue
            if fdate < cutoff:
                continue
            codes = [c.strip() for c in str(items[i] if i < len(items) else "").split(",")
                     if c.strip()]
            desc = ", ".join(_8K_ITEMS.get(c, c) for c in codes) or "material event"
            accn = accns[i].replace("-", "") if i < len(accns) else ""
            doc = docs[i] if i < len(docs) else ""
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
                   if accn and doc else "")
            out.append({
                "headline": f"SEC 8-K: {desc}",
                "source": "SEC EDGAR",
                "datetime": dates[i],
                "url": url,
            })
            if len(out) >= limit:
                break
        return out
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("news_fetcher SEC filings failed for %s — skipped: %s", ticker, exc)
        return []


def gather_ticker_news(
        ticker: str,
        company_name: str = "",
        *,
        finnhub_key: str | None = None,
        alphavantage_key: str | None = None,
        brave_key: str | None = None,
        session: requests.Session | None = None,
        limit: int = 5,
        queries: list[str] | None = None,
        use_alphavantage: bool = True,
        av_max_per_day: int = 20,
) -> list[dict]:
    """Merge every available source into a relevance-filtered, catalyst-first
    headline list for one ticker.

    Google News on the company name is the primary (keyless, right-company);
    Finnhub / AlphaVantage add US depth + sentiment; Yahoo / Brave backstop a
    thin result. Every item is relevance-filtered (drops wrong-company leakage
    like CNQ.TO -> Cenovus) and price-recaps are pushed after catalysts so the
    cap keeps orthogonal news."""
    root = symbol_root(ticker)
    syms, names = company_aliases(ticker, company_name)
    queries = queries or build_queries(ticker, company_name)

    collected: list[dict] = []
    for q in queries[:2]:
        collected += fetch_google_news(q, session=session, limit=limit)
    if finnhub_key:
        collected += fetch_finnhub_news(root, finnhub_key, session=session, limit=limit)
    if (alphavantage_key and use_alphavantage and root.isalpha()
            and _av_budget_ok(av_max_per_day)):
        collected += fetch_alphavantage_news(root, alphavantage_key, session=session, limit=limit)

    relevant = _dedupe_relevant(collected, syms, names)
    # Backstops only when the primaries came up thin — saves keyless calls.
    if len(relevant) < 2:
        collected += _yahoo_ticker_news(ticker, limit, session)
        if brave_key:
            collected += search_ticker_news(ticker, brave_key=brave_key,
                                            session=session, limit=limit)
        relevant = _dedupe_relevant(collected, syms, names)

    # Catalysts first, price-recaps last, then cap — orthogonal news survives.
    relevant.sort(key=lambda h: is_price_recap(str(h.get("headline") or "")))
    return relevant[:limit]


def _dedupe_relevant(items: list[dict], syms: list[str], names: list[str]) -> list[dict]:
    """Drop duplicates (by headline) and off-target items (relevance filter)."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in items:
        head = str(h.get("headline") or h.get("title") or "").strip()
        if not head:
            continue
        key = head.lower()
        if key in seen:
            continue
        # Strict: the headline must name the ticker or company. AlphaVantage's
        # numeric relevance is kept as metadata but never bypasses this — a
        # sector-adjacent article AV rates 0.7-relevant is still not our news.
        if not is_relevant(head, syms, names):
            continue
        # Drop clickbait / listicles / ads / opinion-mill fluff.
        if is_noise(head, str(h.get("source") or "")):
            continue
        seen.add(key)
        out.append(h)
    return out


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
