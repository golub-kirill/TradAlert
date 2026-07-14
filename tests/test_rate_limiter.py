"""
The shared rate limiter must space concurrent callers by min_interval instead of
letting them burst (audit F1). Verified deterministically with a frozen clock:
callers arriving at the same instant get interval-spaced sleep targets.
"""

from __future__ import annotations

import pytest

from core.fetchers import http


def test_concurrent_callers_get_interval_spaced_slots(monkeypatch):
    lim = http._MinIntervalLimiter(0.5)

    # Freeze the clock so all three callers "arrive" at the same instant; capture
    # the sleeps the limiter schedules instead of actually sleeping.
    monkeypatch.setattr(http.time, "monotonic", lambda: 1000.0)
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))

    for _ in range(3):
        lim.wait()

    # First caller fires immediately (no sleep); the next two are pushed out by
    # one and two intervals — spaced, not bursting (the bug would give 0.5, 0.5).
    assert slept == [pytest.approx(0.5), pytest.approx(1.0)]


def test_zero_interval_is_a_noop(monkeypatch):
    lim = http._MinIntervalLimiter(0.0)
    monkeypatch.setattr(http.time, "sleep",
                        lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    lim.wait()  # must not sleep


def test_get_rate_limiter_shares_one_instance_per_key():
    # Same key + interval → the SAME limiter, so concurrent EDGAR/feed callers
    # actually share the throttle instead of each getting a private one.
    a = http.get_rate_limiter("edgar-test", 0.5)
    b = http.get_rate_limiter("edgar-test", 0.5)
    assert a is b
    # A different key is a different limiter.
    assert http.get_rate_limiter("other-test", 0.5) is not a
    # A different interval keeps the SAME limiter (replacing it would reset its
    # reservation clock → burst) and adopts the stricter spacing…
    c = http.get_rate_limiter("edgar-test", 1.0)
    assert c is a
    assert c.min_interval == 1.0
    # …and never loosens back down for a later, laxer caller.
    d = http.get_rate_limiter("edgar-test", 0.2)
    assert d is a
    assert d.min_interval == 1.0


def test_get_rate_limiter_interval_change_keeps_reservation_clock(monkeypatch):
    # The burst the keep-one-instance rule prevents: caller A reserves a slot,
    # caller B (same key, different interval) must be spaced after it — not handed
    # a fresh limiter with no memory of A's reservation.
    a = http.get_rate_limiter("clock-test", 0.5)
    monkeypatch.setattr(http.time, "monotonic", lambda: 1000.0)
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    a.wait()                                        # reserves t=1000.0
    b = http.get_rate_limiter("clock-test", 1.0)    # stricter interval, same clock
    b.wait()                                        # must be pushed out, not burst
    assert slept == [pytest.approx(1.0)]
