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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mysql.connector import Error as MySQLError

from exceptions import ConfigError
from persistence.db_conn import connect as _connect

if TYPE_CHECKING:
    # Type-only import of the shared DTO (no runtime dependency).
    from core.types import TickerResult

logger = logging.getLogger(__name__)

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
                                                    tier,
                                                    review_reason,
                                                    advisor_note,
                                                    score,
                                                    reason,
                                                    close,
                                                    stop_price,
                                                    target_price,
                                                    signal_type,
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
                                  %(tier)s,
                                  %(review_reason)s,
                                  %(advisor_note)s,
                                  %(score)s,
                                  %(reason)s,
                                  %(close)s,
                                  %(stop_price)s,
                                  %(target_price)s,
                                  %(signal_type)s,
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

# Fallback for a DB that predates the advisor_note column (the ALTER is owner-
# applied — the app can't reach prod). Identical to the above minus advisor_note,
# so a fresh column-less deploy still journals every row instead of losing the
# whole batch. Extra keys in the row dicts are harmless for named-param SQL.
_INSERT_SCAN_RESULT_SQL_LEGACY = _INSERT_SCAN_RESULT_SQL.replace(
    "advisor_note,\n", "", 1
).replace("%(advisor_note)s,\n", "", 1)

# MySQL "Unknown column" — signals a pre-migration DB missing advisor_note.
_ERR_BAD_FIELD = 1054


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


def latest_scan_run() -> dict | None:
    """Read-only: the most recent ``scan_runs`` row, for the /status dashboard.

    FAIL-OPEN: any DB error logs a WARNING and returns None (never raises into
    the Telegram daemon).

    Returns
    -------
    dict | None
        ``{"run_id", "created_at", "tickers_scanned", "scan_passed",
           "signals_fired", "market_regime"}`` or None on error / empty table.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, created_at, tickers_scanned, scan_passed, signals_fired, "
            "market_regime FROM scan_runs ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "run_id": row["id"],
            "created_at": row.get("created_at"),
            "tickers_scanned": row.get("tickers_scanned"),
            "scan_passed": row.get("scan_passed"),
            "signals_fired": row.get("signals_fired"),
            "market_regime": row.get("market_regime"),
        }
    except (MySQLError, ConfigError) as exc:
        logger.warning("latest_scan_run read skipped — %s", exc)
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
        try:
            cursor.executemany(_INSERT_SCAN_RESULT_SQL, rows)
        except MySQLError as exc:
            # Pre-migration DB (no advisor_note column): retry column-less so the
            # scan still journals every row instead of losing the whole batch. The
            # advisor_note ALTER is owner-applied; until it lands, degrade quietly.
            if getattr(exc, "errno", None) != _ERR_BAD_FIELD:
                raise
            logger.warning(
                "scan_results missing advisor_note column — journaling without it "
                "(apply the ALTER from data/scan_schema.sql to persist advisor notes)"
            )
            conn.rollback()
            cursor.executemany(_INSERT_SCAN_RESULT_SQL_LEGACY, rows)
        conn.commit()
        inserted = cursor.rowcount
        logger.info(
            "scan_results ← inserted %d row(s) for run_id=%d",
            inserted, run_id,
        )
    except (MySQLError, ConfigError) as exc:
        # Loud, not a warning: a failed insert means the scan fired alerts but
        # journaled nothing, so reconcile_live is blind to those fires. Fail-open
        # (return 0, never abort the scan) but make the loss visible to the
        # operator. Common cause: a missing tier/review_reason column.
        logger.error(
            "scan_results bulk insert FAILED for run_id=%d — live journal is "
            "INCOMPLETE; reconciliation will be blind to this scan's fires: %s",
            run_id, exc,
        )
    finally:
        if conn and conn.is_connected():
            conn.close()

    return inserted


_MARK_DECLINED_SQL = """
                     UPDATE scan_results
                     SET declined = 1
                     WHERE run_id = %(run_id)s
                       AND UPPER(ticker) = UPPER(%(ticker)s) \
                     """


def mark_declined(run_id: int, ticker: str) -> bool:
    """Flag a fired entry as owner-declined (the Telegram 🚫 Skip button).

    Sets ``scan_results.declined = 1`` for (run_id, ticker) so opportunity_tracker
    counts the skipped fire as a passed-on observation (gate='declined'). Returns
    True when a row was updated; fail-open (False, logged) on a DB error.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_MARK_DECLINED_SQL, {"run_id": run_id, "ticker": ticker})
        conn.commit()
        ok = cursor.rowcount >= 1
        if ok:
            logger.info("scan_results ← declined run_id=%d %s", run_id, ticker)
        else:
            logger.warning("mark_declined: no matching scan_results row run_id=%d %s",
                           run_id, ticker)
        return ok
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to mark declined run_id=%d %s — %s", run_id, ticker, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


_STAND_DOWN_SELECT_SQL = """
                         SELECT ticker, passed, signal_kind, tier, reason, error
                         FROM scan_results
                         WHERE run_id = %(run_id)s \
                         """

# Caps keep the readout/Telegram line compact and bounded regardless of run size.
_REJECTION_GATES_TOP = 8
_PASSED_ON_CAP = 25


def stand_down_summary(run_id: int) -> dict | None:
    """
    Read-only rollup of one scan run's ``scan_results`` rows for the stand-down
    readout (stdout + Telegram). Aggregates totals, a per-gate rejection
    breakdown, and the pass-scan-no-fire list.

    FAIL-OPEN: any DB or aggregation error logs a WARNING and returns None;
    never raises into the scan or the Telegram push.

    Returns
    -------
    dict | None
        ``{"run_id", "n_scanned", "n_passed_scan", "n_fired", "n_review",
           "n_errors", "rejection_gates": [{"gate", "n"}, ...],
           "passed_on": [{"ticker", "reason"}, ...]}`` or None on any error.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_STAND_DOWN_SELECT_SQL, {"run_id": run_id})
        rows = cursor.fetchall()  # tuples: (ticker, passed, signal_kind, tier, reason, error)

        n_scanned = len(rows)
        n_passed_scan = 0
        n_fired = 0
        n_review = 0
        n_errors = 0
        gate_counts: dict[str, int] = {}
        passed_on: list[dict] = []

        for ticker, passed, signal_kind, tier, reason, error in rows:
            if error is not None:
                n_errors += 1
            if tier == "NEEDS_REVIEW":
                n_review += 1
            if signal_kind not in (None, "none"):
                n_fired += 1
            if passed:
                n_passed_scan += 1
                if signal_kind in (None, "none") and len(passed_on) < _PASSED_ON_CAP:
                    passed_on.append({"ticker": ticker, "reason": reason})
            else:
                gate = reason if reason else "(unspecified)"
                gate_counts[gate] = gate_counts.get(gate, 0) + 1

        rejection_gates = [
            {"gate": gate, "n": n}
            for gate, n in sorted(
                gate_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:_REJECTION_GATES_TOP]
        ]

        return {
            "run_id": run_id,
            "n_scanned": n_scanned,
            "n_passed_scan": n_passed_scan,
            "n_fired": n_fired,
            "n_review": n_review,
            "n_errors": n_errors,
            "rejection_gates": rejection_gates,
            "passed_on": passed_on,
        }

    except Exception as exc:  # broad: readout is advisory and must never break the scan
        logger.warning("stand_down_summary skipped — %s", exc)
        return None

    finally:
        if conn and conn.is_connected():
            conn.close()


def _result_to_row(run_id: int, r: TickerResult) -> dict:
    """Map one TickerResult to a flat dict matching _INSERT_SCAN_RESULT_SQL."""
    scan = r.scan
    sig = r.signal
    signal_kind = "none"
    if sig and sig.passed:
        signal_kind = {
            "long": "entry_long",
            "short": "entry_short",
            "exit_long": "exit_long",
            "exit_short": "exit_short",
        }.get(sig.direction, "none")

    # Signal geometry — captured only when a real entry/exit fired, so a live
    # signal can later be scored to a forward R and matched to backtest
    # expectancy (see scripts/reconcile_live.py). None for non-signals.
    fired = bool(sig and sig.passed)
    stop_price = float(sig.stop_price) if fired and getattr(sig, "stop_price", None) else None
    target_price = float(sig.target_price) if fired and getattr(sig, "target_price", None) else None
    signal_type = sig.signal_type if fired and getattr(sig, "signal_type", None) else None

    # Operative reason for this ticker's disposition: the scan gate when blocked,
    # else the signal-stage reason when it passed scan but nothing fired (so a
    # "passed scan, no signal" row records WHY no entry, not just the scan-pass note).
    reason = scan.reason or None
    if scan.passed and sig is not None and not sig.passed and getattr(sig, "reason", None):
        reason = sig.reason

    return {
        "run_id": run_id,
        "ticker": r.ticker,
        "passed": int(scan.passed),
        "signal_kind": signal_kind,
        # live data-freshness tier — NEEDS_REVIEW marks a fire on stale/gapped data so
        # reconcile_live.py can exclude it; LIVE (the default) for non-fired/errored rows.
        "tier": sig.tier if sig else "LIVE",
        "review_reason": (sig.review_reason or None) if sig else None,
        # live-only AI advisor note; truncated to the column width, NULL when empty.
        "advisor_note": (getattr(sig, "advisor_note", "")[:512] or None) if sig else None,
        # scan_results.score column retained for historical rows; nothing
        # writes a score anymore, so new rows journal NULL.
        "score": None,
        "reason": reason,
        "close": scan.close,
        "stop_price": stop_price,
        "target_price": target_price,
        "signal_type": signal_type,
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


# ── price alerts (Telegram daemon; journal-only, alerting-only) ────────────────

@dataclass(frozen=True)
class PriceAlert:
    """One owner-set price-cross alert (table ``price_alerts``)."""
    id: int
    ticker: str
    direction: str          # 'above' | 'below'
    price: float


_INSERT_ALERT_SQL = """
                    INSERT INTO price_alerts (ticker, direction, price)
                    VALUES (%(ticker)s, %(direction)s, %(price)s) \
                    """

_LIST_ALERTS_SQL = """
                   SELECT id, ticker, direction, price
                   FROM price_alerts
                   WHERE active = 1
                   ORDER BY id \
                   """

_CANCEL_ALERT_SQL = "UPDATE price_alerts SET active = 0 WHERE id = %(id)s AND active = 1"
_FIRE_ALERT_SQL = ("UPDATE price_alerts SET active = 0, fired_at = CURRENT_TIMESTAMP "
                   "WHERE id = %(id)s AND active = 1")


def add_price_alert(ticker: str, direction: str, price: float) -> int | None:
    """Insert an active price alert; return its new id, or None (logged) on error.

    ``direction`` must be 'above' or 'below' (validated by the caller). Fail-open:
    a DB error returns None rather than raising into the daemon command handler.
    """
    if direction not in ("above", "below"):
        logger.warning("add_price_alert: bad direction %r", direction)
        return None
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_INSERT_ALERT_SQL,
                       {"ticker": ticker.upper(), "direction": direction, "price": float(price)})
        conn.commit()
        new_id = int(cursor.lastrowid)
        logger.info("price_alerts ← #%d %s %s %.4f", new_id, ticker.upper(), direction, price)
        return new_id
    except (MySQLError, ConfigError, TypeError, ValueError) as exc:
        logger.warning("Failed to add price alert %s %s %s — %s", ticker, direction, price, exc)
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


def list_price_alerts() -> list[PriceAlert]:
    """All ACTIVE price alerts (fail-open to [] on a DB error)."""
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_LIST_ALERTS_SQL)
        rows = cursor.fetchall()
        return [PriceAlert(id=int(r[0]), ticker=str(r[1]), direction=str(r[2]), price=float(r[3]))
                for r in rows]
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to list price alerts — %s", exc)
        return []
    finally:
        if conn and conn.is_connected():
            conn.close()


def deactivate_price_alert(alert_id: int, *, fired: bool = False) -> bool:
    """Deactivate an alert (owner /alert del, or on-fire). Returns True if a row changed.

    ``fired`` stamps ``fired_at`` (via the DB clock) so a fired alert is
    distinguishable from an owner-cancelled one. Fail-open (False, logged) on error.
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(_FIRE_ALERT_SQL if fired else _CANCEL_ALERT_SQL, {"id": int(alert_id)})
        conn.commit()
        return cursor.rowcount >= 1
    except (MySQLError, ConfigError) as exc:
        logger.warning("Failed to deactivate price alert #%s — %s", alert_id, exc)
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()


