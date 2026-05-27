"""
Tests for core.fetchers.symbology.to_yf_symbol — the internal→Yahoo ticker
mapping that resolves mis-symboled (not actually delisted) names.
"""

from __future__ import annotations

import pytest

from core.fetchers.symbology import to_yf_symbol


@pytest.mark.parametrize("src,expected", [
    # The operator's case: compound base on a TSX listing.
    ("ABC.DE.TO", "ABC-DE.TO"),
    # US share classes (no exchange suffix) → dash.
    ("BRK.B", "BRK-B"),
    ("BF.B", "BF-B"),
    # Plain exchange listings — suffix preserved, base untouched.
    ("RY.TO", "RY.TO"),
    ("ZQQ.TO", "ZQQ.TO"),
    ("CNQ.TO", "CNQ.TO"),
    ("VOD.L", "VOD.L"),
    # A single German listing keeps its .DE suffix (base has no interior dot).
    ("ABC.DE", "ABC.DE"),
    # Already-clean / non-equity symbols are identity.
    ("SPY", "SPY"),
    ("AAPL", "AAPL"),
    ("BTC-USD", "BTC-USD"),
    ("^VIX", "^VIX"),
    ("", ""),
])
def test_to_yf_symbol(src, expected):
    assert to_yf_symbol(src) == expected


def test_idempotent():
    # Applying twice must not double-convert.
    for s in ["ABC.DE.TO", "BRK.B", "RY.TO", "BTC-USD", "^VIX", "SPY"]:
        once = to_yf_symbol(s)
        assert to_yf_symbol(once) == once, s


def test_whitespace_trimmed():
    assert to_yf_symbol("  BRK.B  ") == "BRK-B"


def test_override_takes_priority():
    from core.fetchers import symbology
    symbology.SUFFIX_OVERRIDES["WEIRD.X"] = "WEIRD-SPECIAL"
    try:
        assert to_yf_symbol("WEIRD.X") == "WEIRD-SPECIAL"
    finally:
        symbology.SUFFIX_OVERRIDES.pop("WEIRD.X", None)
