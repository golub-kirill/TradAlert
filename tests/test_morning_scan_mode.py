"""Morning pre-close scan-mode tests (Vector C-8).

A morning run is blind to today's still-forming bar / the day's open, so every
fired ENTRY is downgraded to NEEDS_REVIEW. Exits must still fire. These cover the
pure downgrade helper ``main._apply_morning_review`` and the ``--morning`` arg.
LIVE-path only — the backtest is unaffected by construction.
"""
from __future__ import annotations

import main
from core.filter_engine import SignalResult


def _entry(direction: str = "long", tier: str = "LIVE", reason: str = "") -> SignalResult:
    return SignalResult(passed=True, direction=direction, signal_type="momentum",
                        tier=tier, review_reason=reason)


def test_morning_downgrades_fired_long_entry():
    s = _entry()
    main._apply_morning_review(s, morning=True)
    assert s.tier == "NEEDS_REVIEW"
    assert "morning scan (pre-close)" in s.review_reason


def test_morning_downgrades_fired_short_entry():
    s = _entry(direction="short")
    main._apply_morning_review(s, morning=True)
    assert s.tier == "NEEDS_REVIEW"
    assert "morning scan (pre-close)" in s.review_reason


def test_no_morning_leaves_entry_live():
    s = _entry()
    main._apply_morning_review(s, morning=False)
    assert s.tier == "LIVE"
    assert s.review_reason == ""


def test_morning_does_not_downgrade_exit_long():
    s = _entry(direction="exit_long")
    main._apply_morning_review(s, morning=True)
    assert s.tier == "LIVE"
    assert s.review_reason == ""


def test_morning_does_not_downgrade_exit_short():
    s = _entry(direction="exit_short")
    main._apply_morning_review(s, morning=True)
    assert s.tier == "LIVE"


def test_morning_appends_to_existing_reason_defensively():
    # Defensive: if a reason is somehow already present on a still-LIVE signal,
    # the morning note is appended, not clobbered.
    s = _entry(tier="LIVE", reason="prior note")
    main._apply_morning_review(s, morning=True)
    assert s.review_reason == "prior note · morning scan (pre-close)"
    assert s.tier == "NEEDS_REVIEW"


def test_morning_already_needs_review_is_noop():
    # tier != LIVE → helper is a no-op (the freshness guard already flagged it;
    # no double-flagging, the existing reason is left untouched).
    s = _entry(tier="NEEDS_REVIEW", reason="gap 2.3×ATR")
    main._apply_morning_review(s, morning=True)
    assert s.review_reason == "gap 2.3×ATR"


def test_morning_helper_is_fail_safe_on_none_and_unfired():
    # A None signal or an unfired one is a no-op and must never raise.
    main._apply_morning_review(None, morning=True)
    s = SignalResult(passed=False, reason="no signal")
    main._apply_morning_review(s, morning=True)
    assert s.tier == "LIVE"


def test_arg_morning_set(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py", "--morning"])
    args = main._parse_args()
    assert args.morning is True


def test_arg_morning_absent_defaults_false(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py"])
    args = main._parse_args()
    assert args.morning is False
