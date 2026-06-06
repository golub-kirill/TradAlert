"""Execution-layer exit rules shared by the backtester and the live scanner.

Keeping the max-hold (time-stop) decision in ONE place guarantees `main.py` and
the backtester agree on when a held trade is force-closed at the swing horizon —
otherwise the live feed and the backtest silently run different exit logic.
"""

from __future__ import annotations


def max_hold_exit_due(
        *,
        bars_held: int,
        current_close: float,
        entry_price: float,
        side: str,
        max_hold_days: int | None,
        mode: str = "hard",
) -> bool:
    """Whether a held trade should be time-stopped at this bar.

    Fires once the trade has been held ``max_hold_days`` trading bars or more. In
    ``"if_not_profit"`` mode it fires only when the position is NOT in profit at
    ``current_close`` (lets winners run to target); ``"hard"`` always fires at the
    cap. Off when ``max_hold_days`` is None. Uses only current-bar information — no
    look-ahead.

    Parameters
    ----------
    bars_held    : Trading bars between entry and the current bar (current - entry).
    current_close: Close of the current/last bar.
    entry_price  : Average entry fill.
    side         : "long" or "short".
    max_hold_days: Cap in trading bars, or None to disable.
    mode         : "hard" or "if_not_profit".
    """
    if max_hold_days is None or bars_held < max_hold_days:
        return False
    in_profit = (current_close < entry_price) if side == "short" else (current_close > entry_price)
    return mode != "if_not_profit" or not in_profit
