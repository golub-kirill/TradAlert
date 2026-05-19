"""
Backtest persistence — backtest_runs + backtest_trades MySQL tables.

Mirrors core.db pattern: fail-open, log warnings, return safe fallbacks.
Schema lives in data/backtest_schema.sql — run once before first use.

The config snapshot column captures the filters.yaml content at run time
so a future re-read knows exactly what rules produced these trades. The
backtest config (start_date, end_date, earnings_aware, etc.) is folded
into the same JSON blob under the '_meta' key by the CLI.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Iterable

import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection

from backtest.stats import Stats
from backtest.trade import Trade

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

# DECIMAL(10,4) max — used to clamp profit_factor when there are no losers.
_PF_INF_SENTINEL: float = 999999.9999

# ── SQL ──────────────────────────────────────────────────────────────────────

_INSERT_RUN_SQL = """
                  INSERT INTO backtest_runs (start_date, end_date, tickers_count, trades_count,
                                             total_r, expectancy_r, profit_factor, win_rate, max_drawdown_r,
                                             config_json, notes)
                  VALUES (%(start_date)s, %(end_date)s, %(tickers_count)s, %(trades_count)s,
                          %(total_r)s, %(expectancy_r)s, %(profit_factor)s, %(win_rate)s,
                          %(max_drawdown_r)s, %(config_json)s, %(notes)s) \
                  """

_INSERT_TRADE_SQL = """
                    INSERT INTO backtest_trades (run_id, ticker, signal_type, direction,
                                                 entry_date, entry_price, initial_stop, initial_target,
                                                 exit_date, exit_price, exit_reason, bars_held, r_multiple,
                                                 market_regime, ticker_trend, entry_score)
                    VALUES (%(run_id)s, %(ticker)s, %(signal_type)s, %(direction)s,
                            %(entry_date)s, %(entry_price)s, %(initial_stop)s, %(initial_target)s,
                            %(exit_date)s, %(exit_price)s, %(exit_reason)s, %(bars_held)s,
                            %(r_multiple)s, %(market_regime)s, %(ticker_trend)s, %(entry_score)s) \
                    """


# ── public API ───────────────────────────────────────────────────────────────

def save_backtest_run(
        start_date: date | None,
        end_date: date | None,
        tickers_count: int,
        stats: Stats,
        config: dict,
        notes: str | None = None,
) -> int | None:
    """
    Insert one row into backtest_runs. Returns the new id, or None on error.

    Parameters
    ----------
    start_date    : From BacktestConfig.start_date. None when full-history.
    end_date      : From BacktestConfig.end_date. None when full-history.
    tickers_count : Number of tickers attempted.
    stats         : Aggregate Stats across all closed trades.
    config        : Snapshot dict — typically filters.yaml plus a _meta key.
    notes         : Optional free-text annotation.
    """
    pf = stats.profit_factor
    if pf == float("inf"):
        pf = _PF_INF_SENTINEL

    row = {
        "start_date": start_date,
        "end_date": end_date,
        "tickers_count": tickers_count,
        "trades_count": stats.trades_count,
        "total_r": round(stats.total_r, 4),
        "expectancy_r": round(stats.expectancy_r, 4),
        "profit_factor": round(pf, 4),
        "win_rate": round(stats.win_rate, 4),
        "max_drawdown_r": round(stats.max_drawdown_r, 4),
        "config_json": json.dumps(config, default=str)[:65000],
        "notes": notes,
    }

    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_INSERT_RUN_SQL, row)
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("backtest_runs ← inserted id=%d  trades=%d  R=%+.2f",
                    new_id, stats.trades_count, stats.total_r)
        return new_id
    except MySQLError as exc:
        logger.warning("backtest_runs write failed — %s", exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def save_backtest_trades(run_id: int, trades: Iterable[Trade]) -> int:
    """
    Bulk-insert closed trades. Returns the number of rows inserted.

    Open trades (Trade.is_closed is False) are filtered out silently.
    """
    rows = [_trade_to_row(run_id, t) for t in trades if t.is_closed]
    if not rows:
        return 0

    conn = None
    inserted = 0
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.executemany(_INSERT_TRADE_SQL, rows)
        conn.commit()
        inserted = cursor.rowcount
        logger.info("backtest_trades ← inserted %d row(s) for run_id=%d",
                    inserted, run_id)
    except MySQLError as exc:
        logger.warning("backtest_trades bulk insert failed — %s", exc)
    finally:
        if conn and conn.is_connected():
            conn.close()
    return inserted


# ── internals ────────────────────────────────────────────────────────────────

def _trade_to_row(run_id: int, t: Trade) -> dict:
    """Map one Trade to a flat dict matching _INSERT_TRADE_SQL."""
    return {
        "run_id": run_id,
        "ticker": t.ticker,
        "signal_type": t.signal_type,
        "direction": t.direction,
        "entry_date": t.entry_date,
        "entry_price": t.entry_price,
        "initial_stop": t.initial_stop,
        "initial_target": t.initial_target,
        "exit_date": t.exit_date,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "bars_held": t.bars_held,
        "r_multiple": round(t.r_multiple, 4),
        "market_regime": t.market_regime,
        "ticker_trend": t.ticker_trend,
        "entry_score": round(t.entry_score, 1),
    }


def _connect() -> PooledMySQLConnection | MySQLConnectionAbstract:
    """Open a MySQL connection from env vars. Same env keys as core.db."""
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        connect_timeout=5,
    )
