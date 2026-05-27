"""
SEC Form 4 (insider transaction) summarizer.

Public API
──────────
``fetch_form4(ticker) -> dict`` returns the per-ticker insider summary
consumed by ``core.scoring._score_insider_buying``::

    {
      "buys_30d":               int,    # number of P (purchase) trades in trailing 30 days
      "buys_90d":               int,
      "sells_90d":              int,
      "buy_value_30d":          float,  # USD notional of buys in 30d
      "sell_value_90d":         float,  # USD notional of sells in 90d
      "distinct_insiders_30d":  int,    # number of unique insider names buying
      "cluster_buy_30d":        bool,   # 3+ distinct insider buys in 30d
      "fetched_at":             str,    # ISO timestamp
    }

Data source
───────────
Best-effort live fetch via ``yfinance.Ticker(ticker).insider_transactions``.
Yahoo's surface aggregates the SEC Form 4 EDGAR feed but loses transaction
codes (P/S) and some price/value resolution. We classify by the
``Transaction`` text field ("Sale", "Purchase", etc.).

For higher-fidelity Form 4 parsing (direct EDGAR XML with strict P/S
codes and per-transaction $ values), see the TODO entry "Form 4 XML
parser: distinguish buys (P) vs sells (S), aggregate $ values."

Failure modes
─────────────
All failures fail-open to a zero-filled dict so ``SignalScorer`` reads
the neutral 0.5 score for this axis. We never raise.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from core.paths import BEHAVIORAL_DIR

import pandas as pd

logger = logging.getLogger(__name__)

_DATA_DIR = BEHAVIORAL_DIR / "form4"
_DEFAULT_STALENESS_DAYS = 1  # Form 4 filings happen daily; cache is mostly
# for collapsing repeated scans within one run.

_ZERO: dict = {
    "buys_30d": 0,
    "buys_90d": 0,
    "sells_90d": 0,
    "buy_value_30d": 0.0,
    "sell_value_90d": 0.0,
    "distinct_insiders_30d": 0,
    "cluster_buy_30d": False,
}


def fetch_form4(
        ticker: str,
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> dict:
    """Return the insider-transaction summary for ``ticker``.

    Reads ``yfinance.Ticker(ticker).insider_transactions`` (a DataFrame of
    recent Form 4 filings), aggregates buys/sells over 30 / 90 day windows
    and writes a JSON cache. Always fail-open.
    """
    data_dir_p = Path(data_dir) if data_dir else _DATA_DIR
    data_dir_p.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir_p / f"{ticker.upper()}.json"

    if not force and _cache_fresh(cache_path, staleness_days):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.debug("[form4] %s loaded from cache", ticker)
            return data
        except (OSError, ValueError) as exc:
            logger.warning("[form4] cache read failed for %s: %s",
                           ticker, exc, exc_info=True)

    try:
        import yfinance as yf
        tx = yf.Ticker(ticker).insider_transactions
    except (ImportError, AttributeError, KeyError, ValueError,
            TypeError, OSError) as exc:
        logger.warning("[form4] live fetch failed for %s: %s",
                       ticker, exc, exc_info=True)
        return _load_cached_or_default(cache_path)

    if tx is None or (isinstance(tx, pd.DataFrame) and tx.empty):
        out = dict(_ZERO, fetched_at=datetime.now().isoformat())
    else:
        try:
            out = _summarise_transactions(tx)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("[form4] summary failed for %s: %s",
                           ticker, exc, exc_info=True)
            return _load_cached_or_default(cache_path)
        out["fetched_at"] = datetime.now().isoformat()

    try:
        cache_path.write_text(json.dumps(out), encoding="utf-8")
    except OSError as exc:
        logger.warning("[form4] cache write failed for %s: %s",
                       ticker, exc, exc_info=True)
    return out


# ── helpers ──────────────────────────────────────────────────────────────────


def _summarise_transactions(tx: pd.DataFrame) -> dict:
    """Aggregate a yfinance insider_transactions DataFrame into the summary dict.

    Yahoo's schema (as of 2026): columns "Insider", "Position",
    "Transaction" (str), "Start Date" (Timestamp), "Ownership", "Shares"
    (int), "Value" (USD, often missing for option exercises).
    The mapping classification:
        - "Purchase", "Buy", "P - Open market purchase" → buy
        - "Sale", "Sell", "S - Open market sale"       → sell
        - "Stock Gift", "Option Exercise", "Stock Award" → ignored
    """
    today = pd.Timestamp.utcnow().tz_localize(None).normalize()
    cutoff_30 = today - timedelta(days=30)
    cutoff_90 = today - timedelta(days=90)

    # Normalise column names defensively — Yahoo has shifted these
    # between releases.
    cols = {c.lower(): c for c in tx.columns}
    date_col = cols.get("start date") or cols.get("date")
    txn_col = cols.get("transaction") or cols.get("type")
    val_col = cols.get("value") or cols.get("transaction value")
    insider_col = cols.get("insider") or cols.get("name")

    if date_col is None or txn_col is None:
        return dict(_ZERO)

    df = tx.copy()
    df["_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["_date"])
    df["_txn"] = df[txn_col].astype(str).str.lower()
    if val_col is not None:
        df["_value"] = pd.to_numeric(df[val_col], errors="coerce").fillna(0.0)
    else:
        df["_value"] = 0.0
    df["_insider"] = (
        df[insider_col].astype(str) if insider_col else ""
    )

    is_buy = df["_txn"].str.contains(r"purchase|\bbuy\b|^p[- ]", regex=True)
    is_sell = df["_txn"].str.contains(r"sale|\bsell\b|^s[- ]", regex=True)

    in_30 = df["_date"] >= cutoff_30
    in_90 = df["_date"] >= cutoff_90

    buys_30 = df.loc[in_30 & is_buy]
    buys_90 = df.loc[in_90 & is_buy]
    sells_90 = df.loc[in_90 & is_sell]

    distinct_30 = buys_30["_insider"].nunique() if not buys_30.empty else 0
    cluster = distinct_30 >= 3 and float(buys_30["_value"].sum()) >= 250_000

    return {
        "buys_30d": int(len(buys_30)),
        "buys_90d": int(len(buys_90)),
        "sells_90d": int(len(sells_90)),
        "buy_value_30d": float(buys_30["_value"].sum()),
        "sell_value_90d": float(sells_90["_value"].sum()),
        "distinct_insiders_30d": int(distinct_30),
        "cluster_buy_30d": bool(cluster),
    }


def _cache_fresh(cache_path: Path, staleness_days: int) -> bool:
    if not cache_path.exists():
        return False
    try:
        mtime = cache_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - mtime) / 86400
        return age_days < staleness_days
    except (OSError, ValueError) as exc:
        logger.debug("[form4] freshness check failed for %s: %s",
                     cache_path, exc)
        return False


def _load_cached_or_default(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("[form4] cached JSON unreadable at %s: %s",
                         cache_path, exc)
    return dict(_ZERO)
