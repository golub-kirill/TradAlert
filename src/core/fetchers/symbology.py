"""
Ticker symbology — map internal/source symbols to Yahoo Finance form.

yfinance uses a hyphen for share-class and compound base symbols, but keeps
the dotted *exchange* suffix. So some tickers that look "delisted" are merely
mis-symboled and silently fail to fetch:

    ABC.DE.TO  -> ABC-DE.TO     (compound base on the TSX)
    BRK.B      -> BRK-B         (US share class, no exchange suffix)
    RY.TO      -> RY.TO         (plain TSX listing — unchanged)
    BTC-USD    -> BTC-USD       (already hyphenated — unchanged)
    ^VIX       -> ^VIX          (index — unchanged)

`to_yf_symbol` strips a known exchange suffix verbatim, converts interior dots
in the *base* to hyphens, then re-attaches the suffix. It is idempotent and the
identity for already-clean symbols, so the existing watchlist (SPY, RY.TO, …)
is unaffected — only previously-failing compound/share-class symbols change.

Public API
    to_yf_symbol(ticker)  -> str
    SUFFIX_OVERRIDES      -> dict   (explicit exotic-case map; takes priority)

Consumed by
    core.fetchers.yf_fetchOne  (the single yfinance call site)
"""

from __future__ import annotations

# Known Yahoo exchange suffixes (kept verbatim, dots intact). Extend as needed.
_EXCHANGE_SUFFIXES: tuple[str, ...] = (
    ".TO", ".V", ".CN", ".NE",  # Canada
    ".L",  # London
    ".DE", ".F", ".BE", ".DU", ".HM", ".MU", ".SG", ".HA",  # Germany
    ".PA", ".AS", ".BR", ".LS", ".MC", ".MI", ".VI", ".SW", ".ST",
    ".OL", ".HE", ".CO", ".IC", ".IR", ".AT", ".LV", ".TL", ".RG",
    ".HK", ".T", ".KS", ".KQ", ".TW", ".TWO", ".SS", ".SZ", ".SI",
    ".AX", ".NZ", ".NS", ".BO", ".SA", ".MX", ".JO", ".TA", ".SR",
)

# Explicit overrides win over the heuristic — for the rare exotic symbol.
SUFFIX_OVERRIDES: dict[str, str] = {}


def to_yf_symbol(ticker: str) -> str:
    """Return the Yahoo Finance form of ``ticker`` (see module docstring)."""
    if not ticker:
        return ticker
    t = ticker.strip()
    if t in SUFFIX_OVERRIDES:
        return SUFFIX_OVERRIDES[t]
    # Indices (^VIX) and already-hyphenated crypto (BTC-USD) need no change.
    if t.startswith("^"):
        return t

    suffix = ""
    upper = t.upper()
    for suf in _EXCHANGE_SUFFIXES:
        if upper.endswith(suf):
            suffix = t[-len(suf):]  # preserve original case
            t = t[: -len(suf)]  # base = everything before suffix
            break

    base = t.replace(".", "-")  # interior dots -> hyphens
    return base + suffix
