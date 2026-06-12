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
from datetime import date
from typing import Iterable

from mysql.connector import Error as MySQLError
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection

from backtest.stats import Stats
from backtest.trade import Trade
from exceptions import ConfigError

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

# DECIMAL(10,4) max — clamps profit_factor when there are no losers.
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
                                                 effective_r, size_mult, borrow_annual_rate,
                                                 mfe_r, mae_r,
                                                 market_regime, ticker_trend, entry_score)
                    VALUES (%(run_id)s, %(ticker)s, %(signal_type)s, %(direction)s,
                            %(entry_date)s, %(entry_price)s, %(initial_stop)s, %(initial_target)s,
                            %(exit_date)s, %(exit_price)s, %(exit_reason)s, %(bars_held)s,
                            %(r_multiple)s, %(effective_r)s, %(size_mult)s, %(borrow_annual_rate)s,
                            %(mfe_r)s, %(mae_r)s,
                            %(market_regime)s, %(ticker_trend)s, %(entry_score)s) \
                    """

# For tables migrated to effective_r but not yet to the excursion columns.
_EXCURSION_KEYS = ("mfe_r", "mae_r")
_INSERT_TRADE_SQL_NO_EXCURSION = """
                    INSERT INTO backtest_trades (run_id, ticker, signal_type, direction,
                                                 entry_date, entry_price, initial_stop, initial_target,
                                                 exit_date, exit_price, exit_reason, bars_held, r_multiple,
                                                 effective_r, size_mult, borrow_annual_rate,
                                                 market_regime, ticker_trend, entry_score)
                    VALUES (%(run_id)s, %(ticker)s, %(signal_type)s, %(direction)s,
                            %(entry_date)s, %(entry_price)s, %(initial_stop)s, %(initial_target)s,
                            %(exit_date)s, %(exit_price)s, %(exit_reason)s, %(bars_held)s,
                            %(r_multiple)s, %(effective_r)s, %(size_mult)s, %(borrow_annual_rate)s,
                            %(market_regime)s, %(ticker_trend)s, %(entry_score)s) \
                    """

# Legacy insert for tables created before the effective_r/size_mult/
# borrow_annual_rate columns existed. Used automatically when the columns are
# absent so journaling never breaks pre-migration (see data/backtest_schema.sql).
_INSERT_TRADE_SQL_LEGACY = """
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
    except (MySQLError, ConfigError) as exc:
        logger.warning("backtest_runs write skipped — %s", exc)
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
        if not _has_column(cursor, "effective_r"):
            # Pre-migration table: drop the new keys and use the legacy column set
            # so journaling still succeeds (run the ALTER in backtest_schema.sql).
            logger.warning("backtest_trades: effective_r columns absent — using legacy "
                           "insert; run the ALTER in data/backtest_schema.sql")
            legacy = [{k: v for k, v in r.items()
                       if k not in ("effective_r", "size_mult", "borrow_annual_rate")
                       + _EXCURSION_KEYS}
                      for r in rows]
            cursor.executemany(_INSERT_TRADE_SQL_LEGACY, legacy)
        elif not _has_column(cursor, "mfe_r"):
            # effective_r-era table without the excursion columns: journal
            # without them (run the mfe_r/mae_r ALTER in backtest_schema.sql).
            logger.warning("backtest_trades: mfe_r/mae_r columns absent — journaling "
                           "without excursions; run the ALTER in data/backtest_schema.sql")
            trimmed = [{k: v for k, v in r.items() if k not in _EXCURSION_KEYS}
                       for r in rows]
            cursor.executemany(_INSERT_TRADE_SQL_NO_EXCURSION, trimmed)
        else:
            cursor.executemany(_INSERT_TRADE_SQL, rows)
        conn.commit()
        inserted = cursor.rowcount
        logger.info("backtest_trades ← inserted %d row(s) for run_id=%d",
                    inserted, run_id)
    except (MySQLError, ConfigError) as exc:
        logger.warning("backtest_trades bulk insert skipped — %s", exc)
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
        # effective_r = r_multiple × size_mult − borrow_drag; this is what sums to
        # backtest_runs.total_r once sizing/shorts are active. r_multiple alone
        # (per-unit-risk) can't reconstruct the headline.
        "effective_r": round(t.effective_r, 4),
        "size_mult": round(t.size_mult, 4),
        "borrow_annual_rate": round(t.borrow_annual_rate, 5),
        # Exit-quality instrumentation: same initial-stop R denominator as
        # r_multiple, so ledger analyses (give-back, WR(T) ceilings) no longer
        # need an engine re-run.
        "mfe_r": round(t.mfe_r, 4),
        "mae_r": round(t.mae_r, 4),
        "market_regime": t.market_regime,
        "ticker_trend": t.ticker_trend,
        "entry_score": round(t.entry_score, 1),
    }


def _has_column(cursor, column: str) -> bool:
    """True when backtest_trades has `column` (migration state probe).

    One cheap information_schema lookup per write lets the writer fall back to an
    older column set instead of failing the whole insert. `column` is always a
    code-supplied literal, never user input.
    """
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'backtest_trades' "
            "AND column_name = %s", (column,)
        )
        row = cursor.fetchone()
        return bool(row and row[0])
    except MySQLError:
        return False


def trade_r_column(cursor) -> str:
    """Return the column reconcilers should aggregate: ``effective_r`` when it
    exists (post-migration; this is what sums to backtest_runs.total_r), else
    ``r_multiple`` for older tables. The return value is a fixed literal, so it is
    safe to interpolate into a query."""
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'backtest_trades' "
            "AND column_name = 'effective_r'"
        )
        row = cursor.fetchone()
        n = list(row.values())[0] if isinstance(row, dict) else row[0]
        return "effective_r" if n else "r_multiple"
    except MySQLError:
        return "r_multiple"


def _run_use_scoring(config_json) -> bool | None:
    """Parse ``_meta.use_scoring`` from a run's config snapshot; None if unknown."""
    if not config_json:
        return None
    try:
        return json.loads(config_json).get("_meta", {}).get("use_scoring")
    except (ValueError, TypeError):
        return None


def reference_run(cursor, run_id=None, prefer_scoring_off: bool = True):
    """Resolve the expectancy-reference backtest_runs row (dictionary cursor).

    Explicit ``run_id`` → that row. Otherwise prefer the latest run tagged
    scoring-OFF (``config_json._meta.use_scoring is False``, matching the live
    default) so the reconciler never silently references a scoring-ON run; fall
    back to the newest run overall. Returns the row dict (without config_json) or
    None.
    """
    cols = "id, start_date, end_date, trades_count, expectancy_r, win_rate, notes"
    if run_id is not None:
        cursor.execute(f"SELECT {cols} FROM backtest_runs WHERE id = %s", (run_id,))
        return cursor.fetchone()
    cursor.execute(f"SELECT {cols}, config_json FROM backtest_runs ORDER BY id DESC LIMIT 50")
    rows = cursor.fetchall()
    if not rows:
        return None
    chosen = None
    if prefer_scoring_off:
        chosen = next((r for r in rows if _run_use_scoring(r.get("config_json")) is False), None)
    chosen = chosen or rows[0]
    chosen.pop("config_json", None)
    return chosen


def hold_range_from_bars(bars, fallback, *, min_samples: int = 8) -> tuple[int, int]:
    """Honest "expected hold" range = (25th, 75th) percentile of actual bars_held.

    No upper clamp: in ``if_not_profit`` mode winners run past the max-hold cap, so
    the real p75 can legitimately exceed it. Falls back to ``fallback`` when there
    are too few samples to be meaningful. Pure (no DB) so it is unit-testable.
    """
    vals = sorted(int(b) for b in bars if b is not None and int(b) > 0)
    if len(vals) < min_samples:
        return fallback
    import statistics
    q = statistics.quantiles(vals, n=4)  # [p25, p50, p75]
    lo = max(1, round(q[0]))
    hi = max(lo, round(q[2]))
    return (lo, hi)


def expected_hold_range(cap: int = 25, fallback: tuple[int, int] | None = None) -> tuple[int, int]:
    """Single source of truth for the displayed expected-hold range.

    The (low, high) shown on charts / Telegram is the 25th–75th percentile of the
    ACTUAL ``bars_held`` from the reference backtest run — so the caption reflects
    how long these trades really last, not a hand-set guess. Display-only: nothing
    in the entry/exit/sizing path reads it. Fail-open to a cap-anchored fallback
    (``cap`` = ``execution.max_hold_days``) when the DB is down or no run exists.
    """
    if fallback is None:
        fallback = (max(1, round(cap * 0.4)), int(cap))
    conn = None
    try:
        conn = _connect()
        cur = conn.cursor(dictionary=True)
        ref = reference_run(cur)
        if ref is None:
            return fallback
        cur.execute(
            "SELECT bars_held FROM backtest_trades "
            "WHERE run_id = %s AND bars_held IS NOT NULL", (ref["id"],)
        )
        bars = [r["bars_held"] for r in cur.fetchall()]
        cur.close()
        return hold_range_from_bars(bars, fallback)
    except (MySQLError, ConfigError) as exc:
        logger.debug("expected_hold_range fell back to %s (%s)", fallback, exc)
        return fallback
    finally:
        if conn and conn.is_connected():
            conn.close()


def _connect() -> PooledMySQLConnection | MySQLConnectionAbstract:
    """Open a MySQL connection. Single source of truth in persistence.db_conn.

    Raises ConfigError when DB env vars are unset (callers catch alongside
    MySQLError to skip journaling gracefully).
    """
    from persistence.db_conn import connect
    return connect()
