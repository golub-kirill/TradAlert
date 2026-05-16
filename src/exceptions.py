"""
TradAlert custom exception hierarchy.

Hierarchy
─────────
    TradAlertError                  base for all TradAlert exceptions
    ├── ValidationError             DataFrame structure or content is invalid
    │   ├── StaleDataError          cached file exceeds the staleness threshold
    │   └── InsufficientDataError   too few rows for the requested operation
    └── FetchError                  data source returned unusable data
"""

from __future__ import annotations


# ── base ──────────────────────────────────────────────────────────────────────

class TradAlertError(Exception):
    """Base class for all TradAlert exceptions."""


# ── validation branch ─────────────────────────────────────────────────────────

class ValidationError(TradAlertError):
    """
    Raised when a DataFrame fails structural or content validation.

    Attributes
    ----------
    detail : str
        Human-readable description of the specific violation.
    ticker : str
        Symbol associated with the bad data. Empty string when unknown.
    """

    def __init__(self, detail: str, ticker: str = "") -> None:
        self.detail = detail
        self.ticker = ticker
        prefix = f"[{ticker}] " if ticker else ""
        super().__init__(f"{prefix}{detail}")


class StaleDataError(ValidationError):
    """
    Raised when a cache file is older than the configured staleness threshold.

    Attributes
    ----------
    hours_old : float
        How old the cache file is, in hours.
    threshold : int
        The staleness threshold that was exceeded, in hours.
    ticker    : str
        Ticker whose cache is stale.
    """

    def __init__(
        self,
        hours_old: float,
        threshold: int,
        ticker: str = "",
    ) -> None:
        self.hours_old = hours_old
        self.threshold = threshold
        detail = (
            f"cache is {hours_old:.1f}h old — exceeds {threshold}h threshold"
        )
        super().__init__(detail=detail, ticker=ticker)


class InsufficientDataError(ValidationError):
    """
    Raised when a DataFrame has too few rows for the requested operation.

    Attributes
    ----------
    got  : int   Actual row count.
    need : int   Minimum required row count.
    """

    def __init__(self, got: int, need: int, ticker: str = "") -> None:
        self.got  = got
        self.need = need
        detail = f"need at least {need} rows, got {got}"
        super().__init__(detail=detail, ticker=ticker)


# ── fetch branch ──────────────────────────────────────────────────────────────

class FetchError(TradAlertError):
    """
    Raised when a data fetcher cannot return usable data.

    Attributes
    ----------
    detail : str
        Human-readable reason for the failure.
    ticker : str
        Symbol that failed to fetch. Empty string when unknown.
    """

    def __init__(self, detail: str, ticker: str = "") -> None:
        self.detail = detail
        self.ticker = ticker
        prefix = f"[{ticker}] " if ticker else ""
        super().__init__(f"{prefix}{detail}")
