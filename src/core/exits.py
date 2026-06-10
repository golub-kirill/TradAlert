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


def trailing_stop_level(
        *,
        side: str,
        highest_high: float | None,
        lowest_low: float | None,
        atr: float | None,
        trail_atr_mult: float | None,
        prev_stop: float | None,
        initial_stop: float,
        mfe_r: float | None = None,
        activate_r: float | None = None,
) -> float | None:
    """New ATR (chandelier) trailing-stop level, ratcheting in the trade's favor.

    Long : ``highest_high - atr*trail_atr_mult`` — stop only moves UP.
    Short: ``lowest_low + atr*trail_atr_mult``   — stop only moves DOWN.

    Returns ``prev_stop`` unchanged when trailing is off (``trail_atr_mult`` None/<=0,
    no usable ATR) or not yet activated (``activate_r`` set and ``mfe_r < activate_r``).
    The floor/ceiling is the INITIAL stop, so the trail never loosens risk; and it
    changes only the exit price/reason — the R denominator stays the initial stop.

    LOOK-AHEAD CONTRACT: callers must compute this at END of a bar (from that bar's
    accumulated extremes) and check it on the NEXT bar, so a bar's own high can't
    set the stop its own low triggers. Uses only current/accumulated info.
    """
    if not trail_atr_mult or trail_atr_mult <= 0 or atr is None or atr <= 0:
        return prev_stop
    if activate_r is not None and (mfe_r is None or mfe_r < activate_r):
        return prev_stop
    base = prev_stop if prev_stop is not None else initial_stop
    if side == "short":
        if lowest_low is None:
            return prev_stop
        candidate = lowest_low + atr * trail_atr_mult
        return min(base, candidate)  # short stop ratchets DOWN, never up
    if highest_high is None:
        return prev_stop
    candidate = highest_high - atr * trail_atr_mult
    return max(base, candidate)  # long stop ratchets UP, never down


def breakeven_stop_level(
        *,
        side: str,
        entry_price: float,
        atr: float | None,
        breakeven_trigger_r: float | None,
        breakeven_buffer_atr: float | None,
        prev_stop: float | None,
        initial_stop: float,
        mfe_r: float | None,
) -> float | None:
    """Move the stop to (around) breakeven once the trade reaches
    ``breakeven_trigger_r`` of favorable excursion — protecting the downside
    WITHOUT capping the upside (unlike a trail, it does not keep moving, so winners
    still run to target).

    Long : stop → entry + breakeven_buffer_atr*ATR ; Short mirrors below entry.
    Ratchets in the trade's favor only and never loosens below the initial stop.
    Returns ``prev_stop`` unchanged when off (trigger None) or not yet reached. The
    R denominator stays the INITIAL stop — this changes only the exit price/reason.
    """
    if breakeven_trigger_r is None or mfe_r is None or mfe_r < breakeven_trigger_r:
        return prev_stop
    buf = (atr * breakeven_buffer_atr) if (atr and atr > 0 and breakeven_buffer_atr) else 0.0
    be = (entry_price - buf) if side == "short" else (entry_price + buf)
    base = prev_stop if prev_stop is not None else initial_stop
    return min(base, be) if side == "short" else max(base, be)
