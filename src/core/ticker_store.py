"""
TickerStore — thin facade over the parquet cache + earnings history.

BarReplayBacktester expects this interface; it keeps the backtester free
of direct persistence imports and makes the data layer swappable in tests.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from core.paths import PRICES_DIR, EARNINGS_HISTORY_DIR

# Lazy imports keep startup fast when only part of the package is used.
_CACHE_DIR = PRICES_DIR
_EARNINGS_DIR = EARNINGS_HISTORY_DIR


class TickerStore:
    """
    Load OHLCV frames and earnings histories from the local cache.

    Parameters
    ----------
    cache_dir : Directory that contains ``{TICKER}.parquet`` files.
    Defaults to ``data/prices``.
    earnings_dir : Directory for ``{TICKER}.json`` earnings history files.
    Defaults to ``data/earnings_history``.
    """

    def __init__(
            self,
            cache_dir: Path | str = _CACHE_DIR,
            earnings_dir: Path | str = _EARNINGS_DIR,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._earnings_dir = Path(earnings_dir)

    # ── public API ────────────────────────────────────────────────────────

    def load_ohlcv(self, ticker: str) -> pd.DataFrame:
        """
        Load the cached OHLCV DataFrame for *ticker*.

        Returns
        -------
        pd.DataFrame
        DatetimeIndex, columns: open high low close volume.

        Raises
        ------
        FileNotFoundError
        When no parquet exists for the ticker.
        """
        from persistence.cache import load as _cache_load
        return _cache_load(ticker, cache_dir=self._cache_dir)

    def get_earnings_history(self, ticker: str) -> list[date]:
        """
        Return every known earnings date for *ticker*, sorted ascending.

        Delegates to ``core.fetchers.earnings_history_store.get_earnings_history``.
        Returns an empty list for ETFs / indices or on any network failure.
        """
        from core.fetchers.earnings_history_store import get_earnings_history
        return get_earnings_history(ticker, cache_dir=self._earnings_dir)


# ── module-level helper ───────────────────────────────────────────────────────

def next_earnings_from(history: list[date], asof: date) -> date | None:
    """
    Return the first earnings date in *history* that is on or after *asof*.

    Thin re-export from the canonical implementation in
    ``core.fetchers.earnings_history`` so BarReplayBacktester can import
    from one place without depending on the backtest package.
    """
    from core.fetchers.earnings_history import next_earnings_from as _canonical
    return _canonical(history, asof)
