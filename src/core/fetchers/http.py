"""
Shared HTTP helpers for external-data fetchers.

Two facilities:

1. ``request_with_retry(method, url, *, retries, backoff, ...)`` — wraps
   ``requests.request`` with exponential backoff on transient failures
   (5xx, 429, ConnectionError, Timeout). Per-host rate limiters can be
   plugged in via the ``rate_limit_key`` argument.

2. ``mask_api_keys_filter`` — a ``logging.Filter`` that masks 32-char API
   keys (FRED format) and Telegram bot tokens in log messages, so a leaked
   secret reads as "<API-KEY-MASKED>" / "<TG-TOKEN-MASKED>" on disk. Install via:

   logger.addFilter(mask_api_keys_filter())
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ── Rate limiter ─────────────────────────────────────────────────────────────

class _MinIntervalLimiter:
    """One-call-per-min_interval limiter shared across threads."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval = float(min_interval_s)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        # Reserve this caller's slot atomically (advance _last under the lock),
        # then sleep OUTSIDE the lock, so concurrent callers get distinct,
        # interval-spaced slots instead of all reading a stale _last and bursting.
        with self._lock:
            now = time.monotonic()
            target = max(now, self._last + self.min_interval)
            self._last = target
        sleep_s = target - now
        if sleep_s > 0:
            time.sleep(sleep_s)


_LIMITERS: dict[str, _MinIntervalLimiter] = {}
_LIMITERS_GUARD = threading.Lock()


def get_rate_limiter(key: str, min_interval_s: float) -> _MinIntervalLimiter:
    """Get-or-create a shared rate limiter for ``key``.

    A key re-requested with a different interval keeps its ONE limiter and adopts
    the stricter (larger) spacing — replacing the instance would reset its
    reservation clock and let two callers hit the host back-to-back.
    """
    with _LIMITERS_GUARD:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = _MinIntervalLimiter(min_interval_s)
            _LIMITERS[key] = limiter
        elif min_interval_s > limiter.min_interval:
            limiter.min_interval = float(min_interval_s)
        return limiter


# ── Retry wrapper ────────────────────────────────────────────────────────────

_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_EXCS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def request_with_retry(
        method: str,
        url: str,
        *,
        retries: int = 3,
        backoff: float = 1.0,
        rate_limit_key: str | None = None,
        rate_limit_interval_s: float = 0.0,
        timeout: float | tuple[float, float] = 30,
        **kwargs: Any,
) -> requests.Response:
    """Issue an HTTP request with exponential backoff on transient failures.

    Parameters
    ----------
    method, url : as for ``requests.request``.
    retries : Max retry attempts after the first call. Total tries
    is ``retries + 1``.
    backoff : Base seconds; doubled each retry (0.5s, 1.0s, 2.0s, ...).
    rate_limit_key : When non-None, share a process-wide rate limiter
    keyed by this string (e.g. ``"sec.gov"``).
    rate_limit_interval_s : Min seconds between calls on the limiter key.
    timeout : Per-request timeout (forwarded to ``requests.request``).
    **kwargs : Additional kwargs for ``requests.request``.

    Raises
    ------
    requests.exceptions.RequestException
    On non-retryable status (4xx that isn't 408/425/429) or after retries
    are exhausted.
    """
    if rate_limit_key:
        limiter = get_rate_limiter(rate_limit_key, rate_limit_interval_s)
    else:
        limiter = None

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if limiter is not None:
            limiter.wait()

        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except _TRANSIENT_EXCS as exc:
            last_exc = exc
            if attempt < retries:
                sleep_s = backoff * (2 ** attempt)
                logger.warning(
                    "[http] %s %s raised %s, retrying in %.1fs (attempt %d/%d)",
                    method, _safe_url(url), type(exc).__name__,
                    sleep_s, attempt + 1, retries,
                )
                time.sleep(sleep_s)
                continue
            raise

        # Check response status for transient errors
        if resp.status_code in _TRANSIENT_STATUS and attempt < retries:
            sleep_s = backoff * (2 ** attempt)
            logger.warning(
                "[http] %s %s → %d, retrying in %.1fs (attempt %d/%d)",
                method, _safe_url(url), resp.status_code,
                sleep_s, attempt + 1, retries,
            )
            time.sleep(sleep_s)
            continue
        return resp

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unexpected end of retry loop")  # unreachable


def _safe_url(url: str) -> str:
    """Strip query strings before logging (defensive — query params
    sometimes carry API keys)."""
    return url.split("?", 1)[0]


# ── Log filter that masks secrets (API keys, bot tokens) ────────────────────

# Lowercase alnum (wider than hex) to also catch rotated/third-party 32-char
# keys; no natural log token is 32 lowercase alnum chars, so no false positives.
_HEX_KEY_PATTERN = re.compile(r"\b[a-z0-9]{32}\b")
# Telegram bot token: numeric bot id, colon, ~35-char base64ish secret. No
# leading \b — the daemon's polling URL glues the id to "bot"
# (…/bot123456789:secret/…), which embeds the token alongside PTB debug output.
_TG_TOKEN_PATTERN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


class _MaskApiKeysFilter(logging.Filter):
    """Mask secrets in log messages: 32-char API keys (FRED) and Telegram bot tokens.

    Install at the root logger in setup_logging so all handlers benefit.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if _HEX_KEY_PATTERN.search(msg) or _TG_TOKEN_PATTERN.search(msg):
                # Store the already-masked string as record.msg and clear args.
                msg = _HEX_KEY_PATTERN.sub("<API-KEY-MASKED>", msg)
                msg = _TG_TOKEN_PATTERN.sub("<TG-TOKEN-MASKED>", msg)
                record.msg = msg
                record.args = ()
        except Exception:
            pass  # filtering must never break logging
        return True


_MASK_FILTER_SINGLETON: _MaskApiKeysFilter | None = None


def mask_api_keys_filter() -> _MaskApiKeysFilter:
    """Return the singleton mask filter (lazy init)."""
    global _MASK_FILTER_SINGLETON
    if _MASK_FILTER_SINGLETON is None:
        _MASK_FILTER_SINGLETON = _MaskApiKeysFilter()
    return _MASK_FILTER_SINGLETON
