"""
Inline-keyboard builders for interactive alerts/cards.

Ships in phase 1 but the push only ATTACHES these when `telegram.daemon_enabled`
is true (otherwise the buttons would be dead until the phase-2 daemon exists to
answer their callback queries). `callback_data` is a compact `verb:args` string
the daemon's callback router parses.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as _Kb


def entry_actions(ticker: str, ref_price: float, stop: float, side: str = "long",
                  run_id: int | None = None) -> _Kb:
    """Buttons under an entry alert: log the fill as a position, or pull a chart.

    "Log opened" opens a fill-source picker (`logmenu`) rather than journaling at
    the alert price directly — a real fill gaps/slips off the alert ref, and an
    honest entry price is what `reconcile_fills` (the live verdict) needs. `side`
    ("long"/"short") rides in the callback so the daemon journals the correct
    direction — a short card must not log as a long (which inverts the risk unit).

    When `run_id` (the scan's id) is supplied a "🚫 Skip" button is added: it
    flags the fired row declined so `opportunity_tracker` scores the passed-on
    outcome (was skipping it the right call?).
    """
    payload = f"{ticker}:{ref_price:.4f}:{stop:.4f}:{side}"
    rows = [[
        _Btn("📈 Log opened", callback_data=f"logmenu:{payload}"),
        _Btn("📊 Chart", callback_data=f"chart:{ticker}"),
    ]]
    if run_id is not None:
        rows.append([_Btn("🚫 Skip", callback_data=f"skip:{run_id}:{ticker}")])
    return _Kb(rows)


def fill_source_menu(ticker: str, ref_price: float, stop: float, side: str = "long") -> _Kb:
    """The fill-price picker shown after "Log opened": live quote, the alert ref,
    or a typed custom price. Each carries the same `TICKER:ref:stop:side` payload
    so the journaled position keeps the correct direction and risk unit.
    """
    payload = f"{ticker}:{ref_price:.4f}:{stop:.4f}:{side}"
    return _Kb([[
        _Btn("💹 @ live", callback_data=f"fill:live:{payload}"),
        _Btn(f"🏷 @ {ref_price:.2f}", callback_data=f"fill:ref:{payload}"),
        _Btn("✍️ Custom", callback_data=f"fill:cust:{payload}"),
    ]])


def position_actions(position_id: int, is_open: bool = True) -> _Kb:
    """Buttons on a position card.

    OPEN: Row 1 = one-tap stop moves (breakeven / lock +1R) + the typed-price stop;
    Row 2 = manage (close / recalc / chart); Row 3 = edit the record. CLOSED: only
    edit + chart make sense (no live stop/close/recalc), so the trade rows drop out.
    """
    pid = position_id
    if not is_open:
        return _Kb([[
            _Btn("✏️ Edit", callback_data=f"editmenu:{pid}"),
            _Btn("📈 Chart", callback_data=f"chartpos:{pid}"),
        ]])
    return _Kb([
        [
            _Btn("🟰 Breakeven", callback_data=f"stopbe:{pid}"),
            _Btn("🔒 +1R", callback_data=f"stop1r:{pid}"),
            _Btn("✏️ Stop", callback_data=f"stop:{pid}"),
        ],
        [
            _Btn("➖ Close…", callback_data=f"closemenu:{pid}"),
            _Btn("🔄 Recalc", callback_data=f"recalc:{pid}"),
            _Btn("📈 Chart", callback_data=f"chartpos:{pid}"),
        ],
        [
            _Btn("✏️ Edit", callback_data=f"editmenu:{pid}"),
        ],
    ])


def edit_menu(position_id: int, is_open: bool = True) -> _Kb:
    """Field picker for ✏️ Edit. Each button force-replies for the new value.

    OPEN positions edit entry / current stop / notes (use Close to set an exit);
    CLOSED positions edit entry / exit / notes (correct a logged fill).
    """
    pid = position_id
    fields = (["entry", "stop", "notes"] if is_open else ["entry", "exit", "notes"])
    row = [_Btn(f"✏️ {f.capitalize()}", callback_data=f"edit:{f}:{pid}") for f in fields]
    row.append(_Btn("✖", callback_data="cancel"))
    return _Kb([row])


def close_menu(position_id: int) -> _Kb:
    """Close/scale picker: take part of the position off (½ / ⅓) or close it Full.

    The partial buttons journal a scale-out at the latest price (manual risk tool);
    "Full" routes to the existing Yes/No close confirm. "✖" dismisses the picker.
    """
    pid = position_id
    return _Kb([[
        _Btn("½", callback_data=f"partial:half:{pid}"),
        _Btn("⅓", callback_data=f"partial:third:{pid}"),
        _Btn("⬛ Full", callback_data=f"close:{pid}"),
        _Btn("✖", callback_data="cancel"),
    ]])


def confirm(action: str, arg: str) -> _Kb:
    """Yes/No confirmation row for destructive actions (e.g. close)."""
    return _Kb([[
        _Btn("✅ Yes", callback_data=f"confirm:{action}:{arg}"),
        _Btn("✖ No", callback_data="cancel"),
    ]])


def status_actions() -> _Kb:
    """Single Refresh button under the /status dashboard (re-renders it in place)."""
    return _Kb([[_Btn("🔄 Refresh", callback_data="status:refresh")]])
