"""
Position CRUD on the positions MySQL table.

Open positions have exit_date IS NULL. Closed positions are retained for
history. All DB errors are caught and logged; functions return safe
fallbacks (empty dict, None) so a DB hiccup never aborts a scan run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Literal

import mysql.connector
from mysql.connector import Error as MySQLError

logger = logging.getLogger(__name__)


Side = Literal["long", "short"]


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
    id:          int
    ticker:      str
    side:        Side
    entry_price: float
    entry_date:  date
    stop_price:  float | None = None
    exit_price:  float | None = None
    exit_date:   date  | None = None
    notes:       str          = ""

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
    SELECT id, ticker, side, entry_price, entry_date,
           stop_price, exit_price, exit_date, notes
    FROM positions
    ORDER BY entry_date DESC
"""

_INSERT_SQL = """
    INSERT INTO positions (ticker, side, entry_price, entry_date, stop_price, notes)
    VALUES (%(ticker)s, %(side)s, %(entry_price)s, %(entry_date)s,
            %(stop_price)s, %(notes)s)
"""

_CLOSE_SQL = """
    UPDATE positions
    SET    exit_price = %(exit_price)s,
           exit_date  = %(exit_date)s
    WHERE  id         = %(id)s
      AND  exit_date IS NULL
"""

_UPDATE_STOP_SQL = """
    UPDATE positions
    SET    stop_price = %(stop_price)s
    WHERE  id         = %(id)s
      AND  exit_date IS NULL
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
        conn   = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_OPEN_SQL)
        rows = cursor.fetchall()
        positions = {r["ticker"]: _row_to_position(r) for r in rows}
        logger.info("Loaded %d open position(s) from positions table", len(positions))
        return positions
    except MySQLError as exc:
        logger.warning("Failed to load open positions — %s", exc)
        return {}
    finally:
        if conn and conn.is_connected():
            conn.close()


def list_all() -> list[Position]:
    """Return every position, newest first. Empty list on DB error."""
    conn = None
    try:
        conn   = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_ALL_SQL)
        return [_row_to_position(r) for r in cursor.fetchall()]
    except MySQLError as exc:
        logger.warning("Failed to list positions — %s", exc)
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def open_position(
    ticker:      str,
    entry_price: float,
    entry_date:  date,
    side:        Side  = "long",
    stop_price:  float | None = None,
    notes:       str          = "",
) -> int | None:
    """
    Insert a new open position. Returns the new id, or None on DB error.
    """
    row = {
        "ticker":      ticker.upper(),
        "side":        side,
        "entry_price": entry_price,
        "entry_date":  entry_date,
        "stop_price":  stop_price,
        "notes":       notes or None,
    }
    conn = None
    try:
        conn   = _connect()
        cursor = conn.cursor()
        cursor.execute(_INSERT_SQL, row)
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("positions ← opened id=%d  %s %s @ %.4f",
                    new_id, side.upper(), ticker.upper(), entry_price)
        return new_id
    except MySQLError as exc:
        logger.warning("Failed to open position for %s — %s", ticker, exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def close_position(
    position_id: int,
    exit_price:  float,
    exit_date:   date,
) -> bool:
    """
    Mark an open position closed. Returns True when one row was updated.
    No-op when the position is already closed or doesn't exist.
    """
    conn = None
    try:
        conn   = _connect()
        cursor = conn.cursor()
        cursor.execute(_CLOSE_SQL, {
            "exit_price": exit_price,
            "exit_date":  exit_date,
            "id":         position_id,
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
    except MySQLError as exc:
        logger.warning("Failed to close position id=%d — %s", position_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


def update_stop(position_id: int, stop_price: float | None) -> bool:
    """Update stop_price on an open position. Returns True on success."""
    conn = None
    try:
        conn   = _connect()
        cursor = conn.cursor()
        cursor.execute(_UPDATE_STOP_SQL, {
            "stop_price": stop_price,
            "id":         position_id,
        })
        conn.commit()
        ok = cursor.rowcount == 1
        if ok:
            logger.info("positions ← stop updated id=%d → %s",
                        position_id,
                        f"{stop_price:.4f}" if stop_price is not None else "NULL")
        return ok
    except MySQLError as exc:
        logger.warning("Failed to update stop id=%d — %s", position_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


# ── internals ─────────────────────────────────────────────────────────────────

def _row_to_position(r: dict) -> Position:
    return Position(
        id          = r["id"],
        ticker      = r["ticker"],
        side        = r["side"],
        entry_price = float(r["entry_price"]),
        entry_date  = r["entry_date"],
        stop_price  = float(r["stop_price"]) if r["stop_price"] is not None else None,
        exit_price  = float(r["exit_price"]) if r["exit_price"] is not None else None,
        exit_date   = r["exit_date"],
        notes       = r["notes"] or "",
    )


def _connect() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host            = os.environ.get("DB_HOST", "localhost"),
        port            = int(os.environ.get("DB_PORT", "3306")),
        user            = os.environ["DB_USER"],
        password        = os.environ["DB_PASSWORD"],
        database        = os.environ["DB_NAME"],
        connect_timeout = 5,
    )
