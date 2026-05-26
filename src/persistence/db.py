"""
MySQL persistence layer.

Reads credentials from environment variables (loaded by dotenv in main.py
before this module is imported):

    DB_HOST      default localhost
    DB_PORT      default 3306
    DB_USER      required
    DB_PASSWORD  required
    DB_NAME      required
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection

from exceptions import ConfigError

if TYPE_CHECKING:
    # Imported for type hints only — avoids a circular import at runtime
    # since main.py already imports from this module.
    from main import TickerResult

logger = logging.getLogger(__name__)

# P1-7 FIX: a missing DB_USER/DB_PASSWORD/DB_NAME used to raise KeyError
# from _connect(), bypassing the surrounding `except MySQLError` and
# crashing the whole pipeline. We now raise ConfigError, and callers
# catch (MySQLError, ConfigError) to degrade gracefully.
_DB_OPTIONAL_KEYS = ("DB_USER", "DB_PASSWORD", "DB_NAME")

# ── SQL ───────────────────────────────────────────────────────────────────────

_INSERT_SCAN_RUN_SQL = """
                       INSERT INTO scan_runs (forced,
                                              tickers_attempted,
                                              tickers_fetched,
                                              tickers_scanned,
                                              scan_passed,
                                              signals_fired,
                                              market_regime,
                                              notes)
                       VALUES (%(forced)s,
                               %(tickers_attempted)s,
                               %(tickers_fetched)s,
                               %(tickers_scanned)s,
                               %(scan_passed)s,
                               %(signals_fired)s,
                               %(market_regime)s,
                               %(notes)s) \
                       """

_INSERT_SCAN_RESULT_SQL = """
                          INSERT INTO scan_results (run_id,
                                                    ticker,
                                                    passed,
                                                    signal_kind,
                                                    score,
                                                    reason,
                                                    close,
                                                    atr,
                                                    atr_pct,
                                                    dv20,
                                                    market_cap,
                                                    rsi,
                                                    macd,
                                                    macd_signal,
                                                    macd_hist,
                                                    error)
                          VALUES (%(run_id)s,
                                  %(ticker)s,
                                  %(passed)s,
                                  %(signal_kind)s,
                                  %(score)s,
                                  %(reason)s,
                                  %(close)s,
                                  %(atr)s,
                                  %(atr_pct)s,
                                  %(dv20)s,
                                  %(market_cap)s,
                                  %(rsi)s,
                                  %(macd)s,
                                  %(macd_signal)s,
                                  %(macd_hist)s,
                                  %(error)s) \
                          """


# ── public API ────────────────────────────────────────────────────────────────

def save_scan_run(
        forced: bool,
        tickers_attempted: int,
        tickers_fetched: int,
        tickers_scanned: int,
        scan_passed: int,
        signals_fired: int,
        market_regime: str | None = None,
        notes: str | None = None,
) -> int | None:
    """
    Insert one row into scan_runs and return the new auto-increment id.

    DB errors are caught and logged — failure returns None and never aborts
    the pipeline.

    Parameters
    ----------
    forced            : True when main.py was run with --force.
    tickers_attempted : Total tickers in the watchlist this run.
    tickers_fetched   : Tickers successfully fetched (cache or network).
    tickers_scanned   : Tickers that reached engine.scan().
    scan_passed       : Tickers that cleared the scan quality gate.
    signals_fired     : Tickers where signal.passed is True.
    market_regime     : First non-empty regime label from any SignalResult.
    notes             : Free-text annotation. Optional.

    Returns
    -------
    int | None
        Auto-increment id of the inserted row, or None on error.
    """
    row = {
        "forced": int(forced),
        "tickers_attempted": tickers_attempted,
        "tickers_fetched": tickers_fetched,
        "tickers_scanned": tickers_scanned,
        "scan_passed": scan_passed,
        "signals_fired": signals_fired,
        "market_regime": market_regime,
        "notes": notes,
    }

    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_INSERT_SCAN_RUN_SQL, row)
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("scan_runs ← inserted id=%d  regime=%s", new_id, market_regime or "—")
        return new_id

    except (MySQLError, ConfigError) as exc:
        logger.warning("scan_runs write skipped — %s", exc)
        return None

    finally:
        if conn and conn.is_connected():
            conn.close()


def save_scan_results(
        run_id: int,
        results: list[TickerResult],
) -> int:
    """
    Bulk-insert one row per TickerResult into scan_results.

    Metric fields are read from ScanResult snapshot fields. Tickers that
    errored before scan() ran will have None for all metric columns; the
    row is still written so the error field is persisted.

    Parameters
    ----------
    run_id  : FK linking these rows to the parent scan_runs row.
    results : List returned by _run_pipeline() in main.py.

    Returns
    -------
    int
        Number of rows actually inserted (0 on error).
    """
    rows = [_result_to_row(run_id, r) for r in results]

    conn = None
    inserted = 0
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.executemany(_INSERT_SCAN_RESULT_SQL, rows)
        conn.commit()
        inserted = cursor.rowcount
        logger.info(
            "scan_results ← inserted %d row(s) for run_id=%d",
            inserted, run_id,
        )
    except (MySQLError, ConfigError) as exc:
        logger.warning("scan_results bulk insert skipped — %s", exc)
    finally:
        if conn and conn.is_connected():
            conn.close()

    return inserted


def _result_to_row(run_id: int, r: TickerResult) -> dict:
    """Map one TickerResult to a flat dict matching _INSERT_SCAN_RESULT_SQL."""
    scan = r.scan
    sig = r.signal
    signal_kind = "none"
    if sig and sig.passed:
        if sig.direction == "long":
            signal_kind = "entry_long"
        elif sig.direction == "exit_long":
            signal_kind = "exit_long"

    return {
        "run_id": run_id,
        "ticker": r.ticker,
        "passed": int(scan.passed),
        "signal_kind": signal_kind,
        "score": sig.score if sig and sig.score > 0 else None,
        "reason": scan.reason or None,
        "close": scan.close,
        "atr": scan.atr,
        "atr_pct": scan.atr_pct,
        "dv20": scan.dv20,
        "market_cap": scan.market_cap,
        "rsi": scan.rsi,
        "macd": scan.macd,
        "macd_signal": scan.macd_signal,
        "macd_hist": scan.macd_hist,
        "error": r.error or None,
    }


def _connect() -> PooledMySQLConnection | MySQLConnectionAbstract:
    """Open a MySQL connection. Raises MySQLError or ConfigError on failure."""
    missing = [k for k in _DB_OPTIONAL_KEYS if not os.environ.get(k)]
    if missing:
        # P1-7 FIX: surface a clean ConfigError instead of KeyError.
        raise ConfigError(
            ", ".join(missing),
            reason="DB env var(s) not set — DB writes disabled",
        )
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        connect_timeout=5,
    )