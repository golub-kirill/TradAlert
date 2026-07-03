#!/usr/bin/env python3
"""
Fetch daily OHLCV for specific tickers into the parquet cache (`data/prices/`).

Reuses the project's own fetch + cache path (`yf_fetchOne.fetch` →
`cache.get_or_fetch`), so symbology, validation, and the on-disk format match
exactly what the backtester loads. Use it to backfill the pruned losers needed by
the survivorship audit (scripts/studies/frozen_universe_ab.py).

    python scripts/fetch/fetch_prices.py MTUM SIZE SPHB EWG EWU USO LIT

`.TO` (TSX) suffixes are fine. Existing fresh files are re-fetched (force=True).
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    tickers = [t for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        print("usage: python scripts/fetch/fetch_prices.py TICKER [TICKER ...]")
        return

    from persistence.cache import get_or_fetch, DEFAULT_CACHE_DIR
    from core.fetchers.yf_fetchOne import fetch as yf_fetch

    # Browser-impersonating session (matches fetcher._fetch_one) to dodge Yahoo
    # bot-blocking; fall back to the default session if curl_cffi is unavailable.
    try:
        from curl_cffi import requests as _rq
        session = _rq.Session(impersonate="chrome")
        fetcher = partial(yf_fetch, session=session)
    except Exception:
        fetcher = yf_fetch

    print(f"  Fetching {len(tickers)} ticker(s) into {DEFAULT_CACHE_DIR} …\n")
    ok, bad = [], []
    for t in tickers:
        try:
            df = get_or_fetch(ticker=t, fetcher=fetcher,
                              cache_dir=DEFAULT_CACHE_DIR, force=True)
            first = df.index[0].date() if len(df) else "—"
            ok.append(t)
            print(f"  ✓ {t:8s} {len(df):>5} bars  (from {first})  -> {t.upper()}.parquet")
        except Exception as exc:
            bad.append(t)
            print(f"  ✗ {t:8s} FAILED: {exc}")

    print(f"\n  Done: {len(ok)} fetched, {len(bad)} failed"
          + (f"  (failed: {', '.join(bad)})" if bad else ""))


if __name__ == "__main__":
    main()
