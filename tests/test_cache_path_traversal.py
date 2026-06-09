"""
Cache path builder must reject path-traversal in a ticker before it becomes a
filename (audit F4 — defense in depth on the owner-gated Telegram chart path),
while still accepting legitimate symbols that contain '.', '-', '^', '='.
"""

from __future__ import annotations

import pytest

from exceptions import ValidationError
from persistence.cache import _path


@pytest.mark.parametrize("bad", ["../etc/passwd", "..\\..\\x", "a/b", "a\\b", "..", ""])
def test_path_rejects_traversal(bad):
    with pytest.raises(ValidationError):
        _path(bad, "data/prices")


@pytest.mark.parametrize("good", ["AAPL", "BRK.B", "^VIX", "CL=F", "ATD.TO", "TEST.1"])
def test_path_allows_legit_symbols(good):
    p = _path(good, "data/prices")
    assert p.name == f"{good.upper()}.parquet"
