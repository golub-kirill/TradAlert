"""
The defaults.py registry must match the actual code fallbacks it documents
(audit F6) — lock the keys so they can't silently diverge.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.defaults import DEFAULTS


def test_breadth_divergence_default_matches_behavioral_fallback(monkeypatch):
    # The registry documents the code fallback at behavioral/__init__.py:246
    # (get("breadth_divergence_penalty", 0.0)). Assert BOTH the registry value and
    # that the live code path applies exactly that default: with empty settings,
    # forcing breadth_divergence must NOT cut size (penalty 0.0). If the code
    # literal drifts from the registry (e.g. -> 0.2) the divergence run shrinks
    # size_multiplier below 0.625 and this turns red.
    from core import behavioral as beh

    assert DEFAULTS.get("settings.behavioral.breadth_divergence_penalty") == 0.0

    beh._BEHAV_STATE_CACHE.clear()
    breadth = pd.DataFrame({"pct_above_50dma": [60.0]}, index=pd.to_datetime(["2024-01-01"]))
    monkeypatch.setattr(beh, "_classify_breadth", lambda b, s: ("WEAK", True))
    st = beh.classify_behavioral_state({"breadth": breadth}, settings={})
    # breadth score 0.5 (sector/positioning missing), penalty 0.0 -> adjusted 0.5
    # -> size = floor 0.25 + (1.0-0.25)*0.5 = 0.625.
    assert st.size_multiplier == pytest.approx(0.625)
