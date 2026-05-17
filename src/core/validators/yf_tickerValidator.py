"""
Ticker-string validator for yfinance requests.

Rules applied (in order)
────────────────────────
    1. Input must be a str
    2. Strip leading / trailing whitespace
    3. Convert to uppercase
    4. Must not be empty after stripping
    5. Must not exceed MAX_TICKER_LENGTH characters
    6. Must match the allowed character set: A-Z, 0-9, '.', '-', '^'
"""

from __future__ import annotations

import re

from exceptions import FetchError

# ── constants ─────────────────────────────────────────────────────────────────

MAX_TICKER_LENGTH: int = 12

_VALID_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z0-9.\-^]+$")


# ── public API ────────────────────────────────────────────────────────────────

def validate_ticker(raw: object) -> str:
    """
    Validate and normalise a ticker symbol for yfinance.

    Parameters
    ----------
    raw : object
        Raw ticker input. Expected type is str; anything else raises.

    Returns
    -------
    str
        Normalised ticker — stripped, uppercased, validated.

    Raises
    ------
    FetchError
        On any validation failure. The exception message identifies the
        violated rule and the input that triggered it.
    """
    if not isinstance(raw, str):
        raise FetchError(
            f"ticker must be a str, got {type(raw).__name__!r}",
            ticker=str(raw),
        )

    ticker = raw.strip().upper()

    if not ticker:
        raise FetchError(
            "ticker is empty or whitespace-only",
            ticker=raw,
        )

    if len(ticker) > MAX_TICKER_LENGTH:
        raise FetchError(
            f"ticker '{ticker}' is {len(ticker)} characters — "
            f"exceeds maximum of {MAX_TICKER_LENGTH}",
            ticker=ticker,
        )

    if not _VALID_PATTERN.match(ticker):
        invalid_chars = sorted({c for c in ticker if not re.match(_VALID_PATTERN, c)})
        raise FetchError(
            f"ticker '{ticker}' contains invalid character(s) {invalid_chars}; "
            f"only A-Z, 0-9, '.', '-', '^' are allowed",
            ticker=ticker,
        )

    return ticker
