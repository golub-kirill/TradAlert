"""
Position CRUD on the ``positions`` MySQL table.

Open positions have ``exit_date IS NULL``. DB errors are caught and logged;
functions return safe fallbacks so a DB hiccup never aborts a scan run.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from mysql.connector import Error as MySQLError

from exceptions import ConfigError, ValidationError
from persistence.db_conn import connect as _connect

logger = logging.getLogger(__name__)

Side = Literal["long", "short"]


# ── risk geometry + open guards ─────────────────────────────────────────────────

def risk_unit(side: str, entry: float, stop: float) -> float:
    """Per-share risk to the initial stop.

    Positive when the stop is on the correct side of entry (below for a long,
    above for a short); zero/negative is a degenerate stop. The single source for
    position risk geometry — shared by the open guard and ``reconcile_fills`` so
    the two can never disagree.
    """
    return (entry - stop) if side == "long" else (stop - entry)


def _is_test_ticker(t: str) -> bool:
    """``TEST`` / ``TEST.1`` / … — example-chart/showcase symbols, never journaled."""
    return t == "TEST" or t.startswith("TEST.")


def validate_open(ticker: str, entry_price: float, side: str,
                  stop_price: float | None, *, open_tickers=()) -> None:
    """Raise ``ValidationError`` when an open would be invalid; else return None.

    Hard rejections: a ``TEST.*`` ticker (showcase only), an unknown side, a
    non-positive/non-finite entry, a stop that is non-positive or on the wrong
    side of entry (which would invert the risk unit — the bug that logged a short
    as a long), and a duplicate open for a ticker that already holds one. A
    *missing* stop is allowed (the caller may add one later) — see ``open_position``.
    """
    t = str(ticker).upper()
    if _is_test_ticker(t):
        raise ValidationError("TEST.* tickers are example/showcase only — not journaled", ticker=t)
    if side not in ("long", "short"):
        raise ValidationError(f"side must be 'long' or 'short', got {side!r}", ticker=t)
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        raise ValidationError(f"entry price must be a number, got {entry_price!r}", ticker=t)
    if not math.isfinite(entry) or entry <= 0:
        raise ValidationError(f"entry price must be > 0, got {entry:g}", ticker=t)
    if stop_price is not None:
        try:
            stop = float(stop_price)
        except (TypeError, ValueError):
            raise ValidationError(f"stop price must be a number, got {stop_price!r}", ticker=t)
        if not math.isfinite(stop) or stop <= 0:
            raise ValidationError(f"stop price must be > 0, got {stop:g}", ticker=t)
        if risk_unit(side, entry, stop) <= 0:
            rel = "below" if side == "long" else "above"
            raise ValidationError(
                f"stop {stop:g} must be {rel} entry {entry:g} for a {side} "
                "(else the risk unit is non-positive)", ticker=t)
    if t in {str(x).upper() for x in open_tickers}:
        raise ValidationError("already has an open position — close it first", ticker=t)


def open_risk_advisory(max_open_risk: float | None, *, open_count: int | None = None) -> str | None:
    """Advisory string when open positions meet/exceed the aggregate-risk cap, else None.

    Counts each open position as ~1R (the validated portfolio's per-trade risk
    unit); a size_mult-weighted refinement is a TODO. ``open_count`` defaults to
    the current number of open positions.
    """
    if not max_open_risk:
        return None
    n = open_count if open_count is not None else len(load_open_positions())
    if n >= max_open_risk:
        return f"{n} open ≥ {max_open_risk:g}R budget — over the validated risk cap"
    return None


# ── dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class Position:
    """
    One row of the positions table.

    Attributes
    ----------
    id          : Auto-increment PK.
    ticker      : Symbol.
    side        : 'long' or 'short'.
    entry_price : Average entry price.
    entry_date  : Date the position was opened.
    stop_price  : Hard stop level, or None when unset.
    exit_price  : Fill on close, or None while open.
    exit_date   : Close date, or None while open.
    notes       : Free-text annotation.
    """
    id: int
    ticker: str
    side: Side
    entry_price: float
    entry_date: date
    stop_price: float | None = None
    exit_price: float | None = None
    exit_date: date | None = None
    notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


# ── SQL ───────────────────────────────────────────────────────────────────────

_SELECT_OPEN_SQL = """
                   SELECT id,
                          ticker,
                          side,
                          entry_price,
                          entry_date,
                          stop_price,
                          exit_price,
                          exit_date,
                          notes
                   FROM positions
                   WHERE exit_date IS NULL
                   ORDER BY entry_date \
                   """

_SELECT_ALL_SQL = """
                  SELECT id,
                         ticker,
                         side,
                         entry_price,
                         entry_date,
                         stop_price,
                         exit_price,
                         exit_date,
                         notes
                  FROM positions
                  ORDER BY entry_date DESC \
                  """

_SELECT_BY_ID_SQL = """
                    SELECT id,
                           ticker,
                           side,
                           entry_price,
                           entry_date,
                           stop_price,
                           exit_price,
                           exit_date,
                           notes
                    FROM positions
                    WHERE id = %(id)s \
                    """

_INSERT_SQL = """
              INSERT INTO positions (ticker, side, entry_price, entry_date, stop_price, notes)
              VALUES (%(ticker)s, %(side)s, %(entry_price)s, %(entry_date)s,
                      %(stop_price)s, %(notes)s) \
              """

_CLOSE_SQL = """
             UPDATE positions
             SET exit_price = %(exit_price)s,
                 exit_date  = %(exit_date)s
             WHERE id = %(id)s
               AND exit_date IS NULL \
             """

_UPDATE_STOP_SQL = """
                   UPDATE positions
                   SET stop_price = %(stop_price)s
                   WHERE id = %(id)s
                     AND exit_date IS NULL \
                   """


# ── public API ────────────────────────────────────────────────────────────────

def load_open_positions() -> dict[str, Position]:
    """
    Return open positions keyed by ticker.

    A ticker can only have one open position at a time. DB failures return
    an empty dict so the pipeline runs without holdings rather than aborting.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_OPEN_SQL)
        rows = cursor.fetchall()
        positions = {r["ticker"]: _row_to_position(r) for r in rows}
        logger.info("Loaded %d open position(s) from positions table", len(positions))
        return positions
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to load open positions — %s", exc)
        return {}
    finally:
        if conn and conn.is_connected():
            conn.close()


def list_all() -> list[Position]:
    """Return every position, newest first. Empty list on DB error."""
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_ALL_SQL)
        return [_row_to_position(r) for r in cursor.fetchall()]
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to list positions — %s", exc)
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def get_position(position_id: int) -> Position | None:
    """Return a single position by id (open or closed), or None when absent.

    A focused lookup for callers that act on one id — the Telegram daemon's
    per-position buttons (stop/close/recalc/chart). None on a missing row or DB
    error so the caller can report "not found" rather than abort.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_BY_ID_SQL, {"id": position_id})
        row = cursor.fetchone()
        return _row_to_position(row) if row else None
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to load position id=%d — %s", position_id, exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def open_position(
        ticker: str,
        entry_price: float,
        entry_date: date,
        side: Side = "long",
        stop_price: float | None = None,
        notes: str = "",
) -> int | None:
    """
    Insert a new open position. Returns the new id, or None on DB error.

    Raises ``ValidationError`` (NOT swallowed) when the open is invalid — a bad
    price, a stop on the wrong side of entry, a bad side, a duplicate open for the
    ticker, or a ``TEST.*`` ticker — so the bad row never enters the journal and
    the caller can surface the reason. A missing stop is allowed but logged (the
    position is unscoreable until a stop is set).
    """
    t = ticker.upper()
    row = {
        "ticker": t,
        "side": side,
        "entry_price": entry_price,
        "entry_date": entry_date,
        "stop_price": stop_price,
        "notes": notes or None,
    }
    conn = None
    try:
        conn = _connect()
        # Read current opens for the duplicate guard, then validate. ValidationError
        # is a different type than (MySQLError, ConfigError), so it propagates past
        # the handler below to the caller rather than being turned into None.
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_OPEN_SQL)
        open_tickers = {r["ticker"] for r in cursor.fetchall()}
        validate_open(t, entry_price, side, stop_price, open_tickers=open_tickers)
        if stop_price is None:
            logger.warning("positions ← %s opened with NO stop — unscoreable until a stop is set", t)

        cursor = conn.cursor()
        cursor.execute(_INSERT_SQL, row)
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("positions ← opened id=%d  %s %s @ %.4f",
                    new_id, side.upper(), t, entry_price)
        return new_id
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to open position for %s — %s", ticker, exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def close_position(
        position_id: int,
        exit_price: float,
        exit_date: date,
) -> bool:
    """
    Mark an open position closed. Returns True when one row was updated.
    No-op when the position is already closed or doesn't exist.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_CLOSE_SQL, {
            "exit_price": exit_price,
            "exit_date": exit_date,
            "id": position_id,
        })
        conn.commit()
        ok = cursor.rowcount == 1
        if ok:
            logger.info("positions ← closed id=%d @ %.4f on %s",
                        position_id, exit_price, exit_date)
        else:
            logger.warning("close_position id=%d affected %d rows",
                           position_id, cursor.rowcount)
        return ok
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to close position id=%d — %s", position_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


def update_stop(position_id: int, stop_price: float | None) -> bool:
    """Update stop_price on an open position. Returns True on success."""
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_UPDATE_STOP_SQL, {
            "stop_price": stop_price,
            "id": position_id,
        })
        conn.commit()
        ok = cursor.rowcount == 1
        if ok:
            logger.info("positions ← stop updated id=%d → %s",
                        position_id,
                        f"{stop_price:.4f}" if stop_price is not None else "NULL")
        return ok
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to update stop id=%d — %s", position_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


# ── internals ─────────────────────────────────────────────────────────────────

def _row_to_position(r: dict) -> Position:
    return Position(
        id=r["id"],
        ticker=r["ticker"],
        side=r["side"],
        entry_price=float(r["entry_price"]),
        entry_date=r["entry_date"],
        stop_price=float(r["stop_price"]) if r["stop_price"] is not None else None,
        exit_price=float(r["exit_price"]) if r["exit_price"] is not None else None,
        exit_date=r["exit_date"],
        notes=r["notes"] or "",
    )


