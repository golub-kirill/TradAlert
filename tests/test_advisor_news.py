"""SEC EDGAR 8-K fetch (fail-open) + the prepend into resolved headlines.

Ported 2026-07-19 from branch advisor-knowledge-critic (b416611); the AV
news-sentiment half of that commit was deliberately NOT ported (AV headlines
already flow through gather_ticker_news; the numeric aggregate was skipped —
see docs/CLEANUP_2026-07-19.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import core.advisor.news_fetcher as nf
from core.advisor.service import AdvisorContext, _resolve_headlines


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, handler):
        self._h = handler

    def get(self, url, **kw):
        return self._h(url)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _old() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=90)).isoformat()


# ── SEC EDGAR 8-K ─────────────────────────────────────────────────────────────

def test_sec_filings_parses_recent_8k(monkeypatch):
    monkeypatch.setattr(nf, "_cik_map", {"AAPL": 320193})
    payload = {"filings": {"recent": {
        "form": ["8-K", "10-Q", "8-K"],
        "filingDate": [_today(), _today(), _old()],
        "items": ["2.02,9.01", "", "5.02"],
        "accessionNumber": ["0000320193-26-000001", "x", "0000320193-26-000002"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm"],
    }}}
    out = nf.fetch_sec_filings("AAPL", session=_Session(lambda u: _Resp(payload)),
                               limit=5, lookback_days=21)
    assert len(out) == 1  # only the recent 8-K (10-Q skipped, old 8-K date-filtered)
    assert "earnings results" in out[0]["headline"] and out[0]["source"] == "SEC EDGAR"
    assert "320193" in out[0]["url"]


def test_sec_filings_non_us_ticker_returns_empty(monkeypatch):
    monkeypatch.setattr(nf, "_cik_map", {"AAPL": 320193})
    assert nf.fetch_sec_filings("RCI-B.TO", session=_Session(lambda u: _Resp({}))) == []


def test_sec_filings_failopen(monkeypatch):
    monkeypatch.setattr(nf, "_cik_map", {"AAPL": 320193})

    def _boom(u):
        raise nf.requests.RequestException("down")

    assert nf.fetch_sec_filings("AAPL", session=_Session(_boom)) == []


# ── prepend into _resolve_headlines ───────────────────────────────────────────

def _ctx(sec: bool) -> AdvisorContext:
    return AdvisorContext(enabled=True, sec_filings=sec, read_only=True)


def test_resolve_prepends_and_dedupes_sec_filings(monkeypatch):
    import core.advisor.service as svc
    monkeypatch.setattr(svc, "gather_ticker_news",
                        lambda *a, **k: [{"headline": "SEC 8-K: earnings results"},
                                         {"headline": "Analyst upgrade"}])
    monkeypatch.setattr(svc, "fetch_sec_filings",
                        lambda t, session=None: [
                            {"headline": "SEC 8-K: earnings results"},   # dupe → dropped
                            {"headline": "SEC 8-K: exec/director change"}])
    heads = _resolve_headlines("AAPL", _ctx(sec=True))
    assert heads[0]["headline"] == "SEC 8-K: exec/director change"       # prepended
    assert sum(1 for h in heads if h["headline"] == "SEC 8-K: earnings results") == 1


def test_resolve_skips_sec_when_disabled(monkeypatch):
    import core.advisor.service as svc
    monkeypatch.setattr(svc, "gather_ticker_news",
                        lambda *a, **k: [{"headline": "Analyst upgrade"}])
    monkeypatch.setattr(svc, "fetch_sec_filings",
                        lambda t, session=None: (_ for _ in ()).throw(AssertionError(
                            "must not be called when sec_filings is off")))
    heads = _resolve_headlines("AAPL", _ctx(sec=False))
    assert heads == [{"headline": "Analyst upgrade"}]
