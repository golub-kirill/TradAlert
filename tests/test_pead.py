"""Unit tests for the PEAD leaf module (src/core/pead).

Pure-logic / pure-math — no network, no I/O. Mirror the validated gate in
scripts/pead_gate.py: classify_session/reaction_index/car_event/qualifies must
match classify_reaction/reaction_pos and the car_event computation there, so the
engine matches the gate exactly. The load-bearing test is the qualifies suite:
it proves TODAY can only fire off prior reactions (no look-ahead).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.pead import (  # noqa: E402
    EarningsEvent, car_event, classify_session, qualifies, reaction_index,
)


def test_classify_session():
    assert classify_session(7) == "BMO"     # 07:00 → before open
    assert classify_session(8) == "BMO"
    assert classify_session(16) == "AMC"    # 16:00 → after close
    assert classify_session(-1) == "AMC"    # unknown → conservative next session
    assert classify_session(12) == "AMC"    # noon boundary → AMC


def test_reaction_index_bmo_vs_amc():
    idx = pd.to_datetime(["2020-01-06", "2020-01-07", "2020-01-08",
                          "2020-01-09", "2020-01-10"])
    ann = pd.Timestamp("2020-01-08")
    # BMO: react ON the announcement date (>=)
    assert reaction_index(idx, ann, "BMO") == 2
    # AMC: react the NEXT session (strictly after)
    assert reaction_index(idx, ann, "AMC") == 3
    # announcement after the last session → no reaction day
    assert reaction_index(idx, pd.Timestamp("2020-02-01"), "AMC") is None


def _flat(n=40, start="2010-01-04", level=100.0):
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({"open": level, "close": level}, index=idx)


def test_car_event_flat_spy_equals_raw_return():
    p = _flat(40)
    idx = p.index
    # +10% jump on idx[20]
    p.loc[idx[20], "close"] = 110.0
    close = p["close"].to_numpy(dtype=float)
    spy_close = pd.Series(100.0, index=idx)   # flat SPY ⇒ market-adjust = 0
    car = car_event(close, idx, spy_close, 20)
    assert car == pytest.approx(0.10, abs=1e-9)
    # no jump elsewhere ⇒ car == 0
    assert car_event(close, idx, spy_close, 10) == pytest.approx(0.0, abs=1e-9)


def test_car_event_iE_below_one_is_nan():
    p = _flat(40)
    close = p["close"].to_numpy(dtype=float)
    spy_close = pd.Series(100.0, index=p.index)
    assert np.isnan(car_event(close, p.index, spy_close, 0))


def _qualifies_fixture(last_car: float, n_priors: int = 8, prior_car: float = 0.02):
    """Flat-then-jumps panel with `n_priors` prior reactions of `prior_car`
    and a reaction landing on the LAST bar with return `last_car`. Returns
    (df, spy_close, events). Flat SPY for determinism."""
    n = 40
    p = _flat(n)
    idx = p.index
    close = np.full(n, 100.0)

    events = []
    # prior reactions spaced across the series, well before the last bar
    prior_positions = list(range(2, 2 + n_priors))  # iE values 2..2+n_priors-1
    for pos in prior_positions:
        close[pos] = close[pos - 1] * (1.0 + prior_car)
        # BMO event on the same session as pos
        events.append(EarningsEvent(date=idx[pos].date(), session="BMO"))

    # reaction on the LAST bar
    iT = n - 1
    close[iT] = close[iT - 1] * (1.0 + last_car)
    events.append(EarningsEvent(date=idx[iT].date(), session="BMO"))

    p["close"] = close
    p["open"] = close
    spy_close = pd.Series(100.0, index=idx)
    return p, spy_close, events


def test_qualifies_fires_high_car():
    df, spy_close, events = _qualifies_fixture(last_car=0.10)
    fires, car_now, reason = qualifies(df, spy_close, events,
                                       min_priors=8, tercile_pct=0.667)
    assert fires is True
    assert car_now == pytest.approx(0.10, abs=1e-6)
    assert "pead car" in reason and ">=" in reason


def test_qualifies_no_fire_low_car():
    # last car BELOW the prior tercile (priors are +2%, last is -5%)
    df, spy_close, events = _qualifies_fixture(last_car=-0.05)
    fires, car_now, reason = qualifies(df, spy_close, events,
                                       min_priors=8, tercile_pct=0.667)
    assert fires is False
    assert car_now == pytest.approx(-0.05, abs=1e-6)
    assert "<" in reason


def test_qualifies_too_few_priors():
    df, spy_close, events = _qualifies_fixture(last_car=0.10, n_priors=3)
    fires, car_now, reason = qualifies(df, spy_close, events,
                                       min_priors=8, tercile_pct=0.667)
    assert fires is False
    assert "priors <" in reason


def test_qualifies_not_a_reaction_day():
    # build the fixture, then drop the event landing on the last bar
    df, spy_close, events = _qualifies_fixture(last_car=0.10)
    events_no_last = events[:-1]
    fires, car_now, reason = qualifies(df, spy_close, events_no_last,
                                       min_priors=8, tercile_pct=0.667)
    assert fires is False
    assert np.isnan(car_now)
    assert reason == "not a reaction day"
