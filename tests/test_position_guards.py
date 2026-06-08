"""Open-position guards: risk geometry, validate_open rejections, budget advisory.

Pure (no DB): exercises position_manager.risk_unit / validate_open / open_risk_advisory
directly with explicit inputs, so the open path can't journal an invalid position.
"""

from __future__ import annotations

import pytest

from core import position_manager as pm
from exceptions import ValidationError


# ── risk geometry (the single source, shared with reconcile_fills) ───────────

def test_risk_unit_sign_by_side():
    assert pm.risk_unit("long", 100.0, 90.0) == 10.0     # stop below entry → +risk
    assert pm.risk_unit("long", 100.0, 110.0) == -10.0   # stop above → inverted
    assert pm.risk_unit("short", 100.0, 110.0) == 10.0   # stop above entry → +risk
    assert pm.risk_unit("short", 100.0, 90.0) == -10.0   # stop below → inverted


# ── validate_open: accepts valid opens ──────────────────────────────────────

def test_validate_open_accepts_valid():
    pm.validate_open("NVDA", 142.55, "long", 134.0, open_tickers=set())   # long, stop below
    pm.validate_open("HMM", 10.0, "short", 11.5, open_tickers=set())      # short, stop above
    pm.validate_open("AAPL", 200.0, "long", None, open_tickers=set())     # missing stop allowed


# ── validate_open: hard rejections ──────────────────────────────────────────

@pytest.mark.parametrize("ticker", ["TEST", "TEST.1", "test.2", "Test.TO"])
def test_rejects_test_tickers(ticker):
    with pytest.raises(ValidationError):
        pm.validate_open(ticker, 100.0, "long", 90.0, open_tickers=set())


def test_rejects_inverted_stop_long_and_short():
    # the XYZ bug: long with the stop above entry → non-positive risk unit
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 88.40, "long", 93.10, open_tickers=set())
    # short with the stop below entry
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 88.40, "short", 80.0, open_tickers=set())


def test_rejects_bad_price_side_and_stop():
    for entry in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            pm.validate_open("AB", entry, "long", None, open_tickers=set())
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 100.0, "sideways", 90.0, open_tickers=set())
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 100.0, "long", 0.0, open_tickers=set())   # stop ≤ 0


def test_rejects_duplicate_open_case_insensitive():
    with pytest.raises(ValidationError):
        pm.validate_open("NVDA", 142.55, "long", 134.0, open_tickers={"NVDA"})
    with pytest.raises(ValidationError):
        pm.validate_open("nvda", 142.55, "long", 134.0, open_tickers={"NVDA"})


def test_rejection_carries_detail_and_ticker():
    with pytest.raises(ValidationError) as ei:
        pm.validate_open("AB", 88.40, "long", 93.10, open_tickers=set())
    assert ei.value.ticker == "AB"
    assert "below entry" in ei.value.detail


# ── budget advisory ─────────────────────────────────────────────────────────

def test_open_risk_advisory():
    assert pm.open_risk_advisory(5.0, open_count=2) is None       # within budget
    assert pm.open_risk_advisory(5.0, open_count=5) is not None   # at the cap
    assert pm.open_risk_advisory(5.0, open_count=6) is not None   # over
    assert pm.open_risk_advisory(None, open_count=99) is None     # no cap configured
    assert pm.open_risk_advisory(0, open_count=99) is None
