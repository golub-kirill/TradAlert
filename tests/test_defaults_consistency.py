"""
defaults.py registry must agree with the actual code fallbacks it documents
(audit F6). Keys had drifted from the code source-of-truth and the
disagreement was silent — lock them so they can't diverge again.
"""

from __future__ import annotations

from core.defaults import DEFAULTS


def test_breadth_divergence_default_matches_behavioral_fallback():
    # behavioral/__init__.py uses get("breadth_divergence_penalty", 0.0).
    assert DEFAULTS.get("settings.behavioral.breadth_divergence_penalty") == 0.0
