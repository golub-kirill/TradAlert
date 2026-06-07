"""
Inline-keyboard builders for interactive alerts/cards.

Ships in phase 1 but the push only ATTACHES these when `telegram.daemon_enabled`
is true (otherwise the buttons would be dead until the phase-2 daemon exists to
answer their callback queries). `callback_data` is a compact `verb:args` string
the daemon's callback router parses.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as _Kb


def entry_actions(ticker: str, ref_price: float, stop: float, side: str = "long") -> _Kb:
    """Buttons under an entry alert: log the fill as a position, or pull a chart.

    `side` ("long"/"short") rides in the callback so the daemon journals the
    correct direction — a short entry card must not log as a long (which would
    invert the risk unit: stop sits above entry for a short).
    """
    return _Kb([[
        _Btn("📈 Log opened", callback_data=f"open:{ticker}:{ref_price:.4f}:{stop:.4f}:{side}"),
        _Btn("📊 Chart", callback_data=f"chart:{ticker}"),
    ]])


def position_actions(position_id: int) -> _Kb:
    """Buttons on an open-position card."""
    return _Kb([[
        _Btn("✏️ Stop", callback_data=f"stop:{position_id}"),
        _Btn("➖ Close", callback_data=f"close:{position_id}"),
        _Btn("🔄 Recalc", callback_data=f"recalc:{position_id}"),
        _Btn("📈 Chart", callback_data=f"chartpos:{position_id}"),
    ]])


def confirm(action: str, arg: str) -> _Kb:
    """Yes/No confirmation row for destructive actions (e.g. close)."""
    return _Kb([[
        _Btn("✅ Yes", callback_data=f"confirm:{action}:{arg}"),
        _Btn("✖ No", callback_data="cancel"),
    ]])
