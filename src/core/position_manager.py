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
    stop_price  : Current/hard stop level, or None when unset. May be moved by
                  update_stop (e.g. trailing).
    initial_stop: The stop recorded at OPEN. Never updated, so it is the stable
                  risk denominator for realized-R reconciliation. Falls back to
                  stop_price for rows opened before this column existed.
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
    initial_stop: float | None = None
    exit_price: float | None = None
    exit_date: date | None = None
    notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class Partial:
    """One partial scale-out against an open position (a manual ½/⅓ close)."""
    id: int
    position_id: int
    exit_price: float
    exit_date: date
    fraction: float


# ── SQL ───────────────────────────────────────────────────────────────────────

_SELECT_OPEN_SQL = """
                   SELECT id,
                          ticker,
                          side,
                          entry_price,
                          entry_date,
                          stop_price,
                          initial_stop,
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
                         initial_stop,
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
                           initial_stop,
                           exit_price,
                           exit_date,
                           notes
                    FROM positions
                    WHERE id = %(id)s \
                    """

_INSERT_SQL = """
              INSERT INTO positions (ticker, side, entry_price, entry_date,
                                     stop_price, initial_stop, notes)
              VALUES (%(ticker)s, %(side)s, %(entry_price)s, %(entry_date)s,
                      %(stop_price)s, %(initial_stop)s, %(notes)s) \
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

_INSERT_PARTIAL_SQL = """
                      INSERT INTO position_partials (position_id, exit_price, exit_date, fraction)
                      VALUES (%(position_id)s, %(exit_price)s, %(exit_date)s, %(fraction)s) \
                      """

_SELECT_PARTIALS_SQL = """
                       SELECT id, position_id, exit_price, exit_date, fraction
                       FROM position_partials
                       WHERE position_id = %(position_id)s
                       ORDER BY id \
                       """


# ── public API ────────────────────────────────────────────────────────────────

def db_reachable() -> bool:
    """True iff a DB connection can be opened.

    Used to NOTIFY the operator when a scan ran with the positions table
    unreadable (open-position awareness lost, scan not journaled) — the scan
    itself still proceeds fail-open; this is detection for an alert, not a guard.
    """
    conn = None
    try:
        conn = _connect()
        return bool(conn.is_connected())
    except (MySQLError, ConfigError):
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


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
        # initial_stop is frozen at open == the stop at entry; update_stop never
        # touches it, so it stays the stable risk denominator for reconciliation.
        "initial_stop": stop_price,
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


# Columns the bot/CLI may edit on a journaled position. Whitelisted so the
# dynamic UPDATE can never inject an arbitrary column name (values are always
# parameterized).
_EDITABLE_COLUMNS = ("entry_price", "stop_price", "initial_stop", "exit_price", "notes")


def update_position(position_id: int, *, entry_price: float | None = None,
                    stop_price: float | None = None, initial_stop: float | None = None,
                    exit_price: float | None = None, notes: str | None = None) -> bool:
    """Edit fields on a journaled position (OPEN or CLOSED). Returns True on update.

    Used to correct a mis-logged fill (wrong entry/exit price), adjust the risk
    denominator, or annotate. Only the passed (non-None) fields change.

    Raises ``ValidationError`` (NOT swallowed) on an invalid edit so the caller can
    surface the reason; returns False on a DB error or a no-op (id not found).
    Safety rules:
      * every price must be finite and > 0;
      * the INITIAL stop (the frozen risk denominator) must stay on the correct
        side of entry (``risk_unit`` > 0) — re-checked when entry or initial_stop
        changes. The current ``stop_price`` is NOT side-constrained (it may trail
        past entry, e.g. a breakeven / +1R move);
      * ``exit_price`` may only be set on a CLOSED position (use ``close_position``
        to close an open one) — prevents the exit_price-set / exit_date-NULL limbo.
    """
    changes = {col: val for col, val in (
        ("entry_price", entry_price), ("stop_price", stop_price),
        ("initial_stop", initial_stop), ("exit_price", exit_price),
        ("notes", notes)) if val is not None}
    if not changes:
        raise ValidationError("no fields to edit")

    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_BY_ID_SQL, {"id": position_id})
        row = cursor.fetchone()
        if row is None:
            raise ValidationError(f"no position #{position_id}")
        pos = _row_to_position(row)

        def _price(label: str, v) -> float:
            """Coerce + validate one price edit → ValidationError on bad input
            (never a raw ValueError, per the documented contract)."""
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValidationError(f"{label} must be a number, got {v!r}")
            if not math.isfinite(v) or v <= 0:
                raise ValidationError(f"{label} must be > 0, got {v:g}")
            return v

        # Coerce + validate each edited price ONCE; notes pass through unchanged.
        coerced: dict = {}
        if entry_price is not None:
            coerced["entry_price"] = _price("entry", entry_price)
        if stop_price is not None:
            coerced["stop_price"] = _price("stop", stop_price)   # positive; may trail past entry
        if initial_stop is not None:
            coerced["initial_stop"] = _price("initial stop", initial_stop)
        if exit_price is not None:
            coerced["exit_price"] = _price("exit", exit_price)
            if pos.is_open:
                raise ValidationError(
                    f"#{position_id} is open — use /close to set an exit")
        if notes is not None:
            coerced["notes"] = notes

        # The initial stop is the risk denominator → must stay on the correct side
        # of entry; re-validate whenever entry or the initial stop moves.
        new_entry = coerced.get("entry_price", pos.entry_price)
        new_init = coerced.get("initial_stop", pos.initial_stop)
        if (entry_price is not None or initial_stop is not None) and new_init is not None:
            if risk_unit(pos.side, new_entry, new_init) <= 0:
                rel = "below" if pos.side == "long" else "above"
                raise ValidationError(
                    f"initial stop {new_init:g} must be {rel} entry {new_entry:g} "
                    f"for a {pos.side} (it is the risk denominator)")

        set_parts, params = [], {"id": position_id}
        for col, val in coerced.items():
            set_parts.append(f"{col} = %({col})s")           # col ∈ _EDITABLE_COLUMNS (whitelisted)
            params[col] = val
        sql = "UPDATE positions SET " + ", ".join(set_parts) + " WHERE id = %(id)s"

        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        ok = cursor.rowcount == 1
        if ok:
            logger.info("positions ← edited id=%d (%s)", position_id, ", ".join(changes))
        return ok
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to edit position id=%d — %s", position_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


# ── partial scale-outs ──────────────────────────────────────────────────────────

def add_partial(position_id: int, exit_price: float, exit_date: date,
                fraction: float) -> int | None:
    """Record a partial scale-out of an OPEN position. Returns the new partial id.

    Raises ``ValidationError`` (NOT swallowed) when the position is missing/closed,
    the fraction is outside (0, 1], or the cumulative scaled-out fraction would
    exceed 1.0 (over-closing) — so the caller can surface the reason and no bad row
    enters the journal. Returns None on a DB error (fail-open, like the other writers).
    The remaining fraction (1 − Σ) closes later via ``close_position``; reconcile
    weights realized R by these fractions.
    """
    try:
        frac = float(fraction)
    except (TypeError, ValueError):
        raise ValidationError(f"fraction must be a number, got {fraction!r}")
    if not (0.0 < frac <= 1.0):
        raise ValidationError(f"fraction must be in (0, 1], got {frac:g}")
    conn = None
    try:
        conn = _connect()
        # ValidationError (open-state / over-scale) is a different type than
        # (MySQLError, ConfigError), so it propagates to the caller rather than
        # being turned into None by the handler below.
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_BY_ID_SQL, {"id": position_id})
        prow = cursor.fetchone()
        if prow is None or prow["exit_date"] is not None:
            raise ValidationError(f"no open position #{position_id} to scale out")
        cursor.execute(_SELECT_PARTIALS_SQL, {"position_id": position_id})
        used = sum(float(r["fraction"]) for r in cursor.fetchall())
        if used + frac > 1.0 + 1e-6:
            raise ValidationError(
                f"scaling {frac:g} would exceed the position "
                f"({used:g} already scaled out)")
        cursor = conn.cursor()
        cursor.execute(_INSERT_PARTIAL_SQL, {
            "position_id": position_id, "exit_price": exit_price,
            "exit_date": exit_date, "fraction": frac})
        conn.commit()
        new_id = cursor.lastrowid
        logger.info("position_partials ← #%d scaled %.4f @ %.4f on %s",
                    position_id, frac, exit_price, exit_date)
        return new_id
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to add partial for #%d — %s", position_id, exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def get_partials(position_id: int) -> list[Partial]:
    """All partial scale-outs for a position, oldest first. Empty on DB error."""
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(_SELECT_PARTIALS_SQL, {"position_id": position_id})
        return [Partial(id=r["id"], position_id=r["position_id"],
                        exit_price=float(r["exit_price"]), exit_date=r["exit_date"],
                        fraction=float(r["fraction"])) for r in cursor.fetchall()]
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to load partials for #%d — %s", position_id, exc)
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def remaining_fraction(position_id: int) -> float:
    """Fraction of the position still open = 1 − Σ partial fractions (clamped ≥ 0)."""
    used = sum(p.fraction for p in get_partials(position_id))
    return max(0.0, round(1.0 - used, 6))


# ── internals ─────────────────────────────────────────────────────────────────

def _row_to_position(r: dict) -> Position:
    return Position(
        id=r["id"],
        ticker=r["ticker"],
        side=r["side"],
        entry_price=float(r["entry_price"]),
        entry_date=r["entry_date"],
        stop_price=float(r["stop_price"]) if r["stop_price"] is not None else None,
        initial_stop=float(r["initial_stop"]) if r.get("initial_stop") is not None else None,
        exit_price=float(r["exit_price"]) if r["exit_price"] is not None else None,
        exit_date=r["exit_date"],
        notes=r["notes"] or "",
    )


