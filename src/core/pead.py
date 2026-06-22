"""
Post-earnings-drift (PEAD) signal math.

A leaf module holding the point-in-time PEAD entry logic, mirroring the
already-validated gate in ``scripts/pead_gate.py`` (``classify_reaction``,
``reaction_pos``, and the ``car_event`` computation inside
``build_ticker_panel``) so the engine matches the gate exactly. It must not
import the engine, ``main``, or anything from ``core.filter_engine`` — only
numpy / pandas / dataclasses / datetime / logging.

Public API
----------
EarningsEvent
    Frozen DTO: an earnings announcement date + reaction session ('BMO'/'AMC').
classify_session(local_hour)
    Map an announcement hour (exchange-local) to 'BMO' or 'AMC'.
reaction_index(price_index, ann_date, session)
    Integer position of the reaction day E in a sorted price DatetimeIndex.
car_event(close, dates, spy_close, iE)
    Market-adjusted close-to-close announcement abnormal return on day E.
qualifies(df, spy_close, events, *, min_priors, tercile_pct)
    Decide whether TODAY (the last bar of ``df``) is a qualifying PEAD long.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "EarningsEvent",
    "classify_session",
    "reaction_index",
    "car_event",
    "qualifies",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EarningsEvent:
    """One earnings announcement.

    Attributes
    ----------
    date    : Announcement date.
    session : Reaction session — 'BMO' (priced same session) or 'AMC' (next session).
    """
    date: datetime.date
    session: str


def classify_session(local_hour: int) -> str:
    """Map an announcement hour (exchange-local) to the reaction session.

    BMO (before ~noon) → the market prices it in the announcement-date session.
    AMC (>=noon) or unknown (-1) → the NEXT session (conservative, no look-ahead).
    Mirrors ``pead_gate.classify_reaction``.
    """
    h = int(local_hour)
    if 0 <= h < 12:
        return "BMO"
    return "AMC"


def reaction_index(price_index: pd.DatetimeIndex, ann_date: pd.Timestamp,
                   session: str) -> int | None:
    """Integer position of the reaction day E in a sorted price DatetimeIndex.

    BMO → first index position on/after the announcement date (searchsorted
    side='left'); AMC → first position strictly after it (side='right'). Returns
    None if the position is past the end of the index. Mirrors
    ``pead_gate.reaction_pos`` (operating on the numpy ``.values``).
    """
    dates = price_index.values
    side = "left" if session == "BMO" else "right"  # >= vs >
    i = int(np.searchsorted(dates, np.datetime64(ann_date), side=side))
    return i if i < len(dates) else None


def car_event(close: np.ndarray, dates: pd.DatetimeIndex, spy_close: pd.Series,
              iE: int) -> float:
    """Announcement abnormal return on reaction day E, market-adjusted vs SPY.

    ``(close[iE]/close[iE-1] - 1) - (spy.asof(dates[iE])/spy.asof(dates[iE-1]) - 1)``.
    Returns NaN if iE < 1, or any of the four prices is non-finite or <= 0.
    Matches the market-adjusted close-to-close reaction return in
    ``pead_gate.build_ticker_panel``.
    """
    if iE < 1:
        return float("nan")
    c_now, c_prev = close[iE], close[iE - 1]
    spy_now = spy_close.asof(dates[iE])
    spy_prev = spy_close.asof(dates[iE - 1])
    vals = (c_now, c_prev, spy_now, spy_prev)
    if not all(np.isfinite(v) and v > 0 for v in vals):
        return float("nan")
    return (c_now / c_prev - 1.0) - (spy_now / spy_prev - 1.0)


def qualifies(df: pd.DataFrame, spy_close: pd.Series, events: list[EarningsEvent],
              *, min_priors: int, tercile_pct: float) -> tuple[bool, float, str]:
    """Decide whether TODAY (the last bar of ``df``) is a qualifying PEAD long.

    Point-in-time, no look-ahead: TODAY fires only if it is a reaction day E for
    one of the events and its market-adjusted reaction return clears the tercile
    cutoff of the prior reactions (all strictly before today).

    Returns ``(fires, car_now, reason)``.
    """
    close = df["close"].to_numpy(dtype=float)
    dates = df.index
    iT = len(df) - 1

    pairs = [(ev, reaction_index(dates, pd.Timestamp(ev.date), ev.session))
             for ev in events]

    today_is_reaction = any(ri == iT for _, ri in pairs)
    if not today_is_reaction:
        return (False, float("nan"), "not a reaction day")

    car_now = car_event(close, dates, spy_close, iT)
    if not np.isfinite(car_now):
        return (False, car_now, "car unavailable")

    priors = []
    for _, ri in pairs:
        if ri is not None and 1 <= ri < iT:
            c = car_event(close, dates, spy_close, ri)
            if np.isfinite(c):
                priors.append(c)

    if len(priors) < min_priors:
        return (False, car_now, f"{len(priors)} priors < {min_priors}")

    cutoff = float(np.quantile(np.asarray(priors), tercile_pct))
    fires = bool(car_now >= cutoff)
    reason = (f"pead car {car_now:+.4f} {'>=' if fires else '<'} "
              f"p{tercile_pct:.0%} cutoff {cutoff:+.4f} (n={len(priors)})")
    return (fires, car_now, reason)
