"""
TradAlert custom exception hierarchy.

    TradAlertError                  base class for the tree
    ├── ValidationError             DataFrame structure or content is invalid
    │   ├── InsufficientDataError   too few rows for the requested operation
    │   └── DataStalenessError      live bar still behind the last session after refetch
    ├── FetchError                  data source returned unusable data
    └── ConfigError                 YAML config key missing or wrong type
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


class InsufficientDataError(ValidationError):
    """
    Raised when a DataFrame has too few rows for the requested operation.

    Attributes
    ----------
    got  : int   Actual row count.
    need : int   Minimum required row count.
    """

    def __init__(self, got: int, need: int, ticker: str = "") -> None:
        self.got = got
        self.need = need
        detail = f"need at least {need} rows, got {got}"
        super().__init__(detail=detail, ticker=ticker)


class DataStalenessError(ValidationError):
    """
    Signals that a ticker's DATA (its most recent bar) is still behind the last completed
    exchange session AFTER a refetch attempt — the engine would otherwise evaluate a signal
    on bars blind to one or more sessions (weekend/overnight news). This is the bar
    *timestamp* in trading days (not cache *file* age), on the LIVE path only.

    NOTE: the live scanner does NOT raise this today — a stale-after-refetch (or gapped) fire
    is DOWNGRADED to ``SignalResult.tier = "NEEDS_REVIEW"`` (``main._mark_review``) so it is
    surfaced for review rather than dropped. This type is retained for callers that would
    rather treat staleness as a hard error (see ``core.freshness`` + the live data-freshness
    hardening).

    Attributes
    ----------
    last_bar        : date  Most recent bar present in the data.
    sessions_behind : int   Completed sessions the data is behind the last close (>= 1).
    """

    def __init__(self, last_bar, sessions_behind: int, ticker: str = "") -> None:
        self.last_bar = last_bar
        self.sessions_behind = sessions_behind
        detail = (f"data ends {last_bar} — {sessions_behind} completed session(s) behind the "
                  f"last close (still stale after refetch)")
        super().__init__(detail=detail, ticker=ticker)


# ── fetch branch ──────────────────────────────────────────────────────────────

class FetchError(TradAlertError):
    """
    Raised when a data fetcher cannot return usable data.

    Attributes
    ----------
    detail : str
        Reason for the failure.
    ticker : str
        Symbol that failed to fetch. Empty string when unknown.
    """

    def __init__(self, detail: str, ticker: str = "") -> None:
        self.detail = detail
        self.ticker = ticker
        prefix = f"[{ticker}] " if ticker else ""
        super().__init__(f"{prefix}{detail}")


# ── config branch ─────────────────────────────────────────────────────────────

class ConfigError(TradAlertError):
    """
    Raised when a YAML config value is missing or has the wrong type.

    Attributes
    ----------
    dotted      : str  Dotted path to the offending key.
    reason      : str  Specific violation.
    missing_key : str  Alias for ``dotted``.
    """

    def __init__(self, dotted: str, *, reason: str) -> None:
        self.dotted = dotted
        self.reason = reason
        self.missing_key = dotted
        super().__init__(f"config key {dotted}: {reason}")
