"""
The defaults.py registry must match the actual code fallbacks it documents
(audit F6) — lock the keys so they can't silently diverge.
"""

from __future__ import annotations

from core.defaults import DEFAULTS


def test_breadth_divergence_default_matches_behavioral_fallback():
    # behavioral/__init__.py uses get("breadth_divergence_penalty", 0.0).
    assert DEFAULTS.get("settings.behavioral.breadth_divergence_penalty") == 0.0
