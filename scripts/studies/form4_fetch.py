#!/usr/bin/env python3
"""
SEC EDGAR Form-4 fetcher (point-in-time insider transactions).

Pulls every non-derivative Form-4 transaction for the US single-stocks of `tier_a`
(those with an EDGAR CIK; ETFs and `.TO` names have no Form-4 — see
`docs/backtest_out/form4_gate_prereg.md`) and caches them per ticker to
`data/behavioral/form4/{TICKER}.parquet`. RESUMABLE: a ticker whose parquet already
exists is skipped, so an orphaned run just re-runs and continues
([[heavy-runs-orphan-from-agent]]). Point-in-time stamp = `filingDate` (not the txn
date). SEC fair-access: a descriptive User-Agent and a ≤ ~9 req/s global rate cap.

    .venv/Scripts/python.exe scripts/studies/form4_fetch.py                 # all 86 US single-stocks
    .venv/Scripts/python.exe scripts/studies/form4_fetch.py --tickers AAPL,XOM --workers 1   # sample

The XML parser (`parse_ownership`) is a pure, import-safe helper (tests/test_form4_fetch.py).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

import pandas as pd  # noqa: E402
import yaml          # noqa: E402

UA = "TradAlert research foxxx2game@gmail.com"   # SEC requires a descriptive contact UA
CACHE_DIR = _ROOT / "data" / "behavioral" / "form4"


# ── polite HTTP ─────────────────────────────────────────────────────────────────

class _RateLimiter:
    """Thread-safe minimum-interval gate (≈ rps requests/sec across all workers)."""
    def __init__(self, rps: float):
        self._min = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self._min


def _http_get(url: str, rate: _RateLimiter, retries: int = 4) -> bytes:
    last = None
    for attempt in range(retries):
        rate.wait()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            time.sleep(1.5 * (attempt + 1))   # 429/5xx → back off
        except Exception as e:                 # noqa: BLE001 — network flakiness, retry
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries}: {url} ({last})")


# ── pure parser ───────────────────────────────────────────────────────────────

def parse_ownership(submission_text: str, filing_date: str,
                    acceptance_dt: str) -> list[dict]:
    """Parse non-derivative transactions from a Form-4 full-submission .txt.

    Extracts the ``<ownershipDocument>…</ownershipDocument>`` block (robust across the
    SGML wrapper and the 2003→now naming changes) and returns one dict per
    ``nonDerivativeTransaction`` with the transaction code, acquired/disposed flag,
    shares, price, txn date, and reporting-owner CIK. Holdings-only or derivative-only
    filings return ``[]``. Pure / import-safe — no I/O.
    """
    s = submission_text.find("<ownershipDocument")
    e = submission_text.find("</ownershipDocument>")
    if s == -1 or e == -1:
        return []
    block = submission_text[s:e + len("</ownershipDocument>")]
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return []
    symbol = (root.findtext(".//issuerTradingSymbol") or "").strip().upper()
    owners = [
        (ro.findtext(".//rptOwnerCik") or "").strip()
        for ro in root.findall(".//reportingOwner")
    ]
    owner_cik = owners[0] if owners else ""
    rows = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = (t.findtext(".//transactionCoding/transactionCode") or "").strip()
        ad = (t.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
              or "").strip()
        shares = t.findtext(".//transactionAmounts/transactionShares/value")
        price = t.findtext(".//transactionAmounts/transactionPricePerShare/value")
        tdate = (t.findtext(".//transactionDate/value") or "").strip()
        try:
            sh = float(shares) if shares not in (None, "") else 0.0
        except ValueError:
            sh = 0.0
        try:
            pr = float(price) if price not in (None, "") else 0.0
        except ValueError:
            pr = 0.0
        rows.append(dict(
            symbol=symbol, filing_date=filing_date, acceptance_dt=acceptance_dt,
            txn_date=tdate, owner_cik=owner_cik, code=code, ad=ad,
            shares=sh, price=pr, value=sh * pr,
        ))
    return rows


# ── EDGAR navigation ─────────────────────────────────────────────────────────────

def load_cik_map(rate: _RateLimiter) -> dict[str, int]:
    cache = CACHE_DIR / "_cik_map.json"
    if cache.exists():
        return {k: int(v) for k, v in json.loads(cache.read_text()).items()}
    m = json.loads(_http_get("https://www.sec.gov/files/company_tickers.json", rate))
    out = {row["ticker"].upper(): int(row["cik_str"]) for row in m.values()}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def iter_form4_filings(cik: int, rate: _RateLimiter):
    """Yield (accession_nodash, accession_dashed, filingDate, acceptanceDateTime) for
    every Form-4, across the 'recent' block and the older paged submission files."""
    def _emit(form, acc, fdate, adt):
        for i in range(len(form)):
            if form[i] == "4":
                a = acc[i]
                yield a.replace("-", ""), a, fdate[i], adt[i]

    sub = json.loads(_http_get(
        f"https://data.sec.gov/submissions/CIK{cik:010d}.json", rate))
    rec = sub["filings"]["recent"]
    yield from _emit(rec["form"], rec["accessionNumber"], rec["filingDate"],
                     rec["acceptanceDateTime"])
    for f in sub["filings"].get("files", []):
        pg = json.loads(_http_get(f"https://data.sec.gov/submissions/{f['name']}", rate))
        yield from _emit(pg["form"], pg["accessionNumber"], pg["filingDate"],
                         pg.get("acceptanceDateTime", pg["filingDate"]))


def fetch_ticker(ticker: str, cik: int, rate: _RateLimiter) -> tuple[str, int, int]:
    """Fetch + parse every Form-4 for one ticker, write the per-ticker parquet cache.
    Returns (ticker, n_filings, n_txns). Skips if already cached."""
    out = CACHE_DIR / f"{ticker}.parquet"
    if out.exists():
        try:
            return ticker, -1, len(pd.read_parquet(out))   # -1 filings = cached
        except Exception:
            pass
    rows: list[dict] = []
    n_filings = 0
    for acc_nd, acc_d, fdate, adt in iter_form4_filings(cik, rate):
        n_filings += 1
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/{acc_d}.txt"
        try:
            txt = _http_get(url, rate).decode("utf-8", "replace")
        except urllib.error.HTTPError:
            continue
        rows.extend(parse_ownership(txt, fdate, adt))
    df = pd.DataFrame(rows, columns=["symbol", "filing_date", "acceptance_dt",
                                     "txn_date", "owner_cik", "code", "ad",
                                     "shares", "price", "value"])
    df.insert(0, "ticker", ticker)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(out)                                   # atomic write
    return ticker, n_filings, len(df)


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="EDGAR Form-4 fetcher (US tier_a single-stocks)")
    ap.add_argument("--tickers", default=None, help="comma list override (else all CIK-covered US tier_a)")
    ap.add_argument("--max-tickers", type=int, default=None, help="cap ticker count (sampling)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rps", type=float, default=9.0, help="global EDGAR request cap")
    args = ap.parse_args()

    rate = _RateLimiter(args.rps)
    cikmap = load_cik_map(rate)

    if args.tickers:
        want = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        wl = yaml.safe_load(open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8"))
        tier = [x for x in wl.get("tier_a", []) if isinstance(x, str)]
        want = [x for x in tier if "." not in x and x not in ("SPY", "QQQ", "^VIX")]
    pairs = [(t, cikmap[t]) for t in want if t in cikmap]   # CIK-covered only (drops ETFs)
    if args.max_tickers:
        pairs = pairs[:args.max_tickers]
    print(f"  Form-4 fetch: {len(pairs)} CIK-covered tickers "
          f"({len(want) - len(pairs)} of {len(want)} dropped — no CIK / ETF), "
          f"workers={args.workers} rps={args.rps}", flush=True)

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_ticker, t, c, rate): t for t, c in pairs}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                tk, nf, nt = fut.result()
                done += 1
                tag = "cached" if nf == -1 else f"{nf} filings"
                print(f"  [{done}/{len(pairs)}] {tk:<8} {tag:>12} → {nt} txns "
                      f"({time.time() - t0:.0f}s)", flush=True)
            except Exception as e:   # noqa: BLE001
                print(f"  [ERR] {t}: {e}", flush=True)
    print(f"  done: {done}/{len(pairs)} tickers in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
