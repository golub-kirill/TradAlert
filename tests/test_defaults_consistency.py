"""
defaults.py registry must agree with the actual code fallbacks it documents
(audit F6). Two keys had drifted — min_score 50 vs the code's 60, breadth penalty
0.2 vs the code's 0.0 — and neither was consumed, so the disagreement was silent.
Lock them to the code source-of-truth so they can't diverge again.
"""

from __future__ import annotations

from core.defaults import DEFAULTS
from core.scoring import _DEFAULT_MIN_SCORE


def test_min_score_default_matches_scoring_constant():
    assert DEFAULTS.get("settings.scanner.min_score_to_alert") == _DEFAULT_MIN_SCORE


def test_breadth_divergence_default_matches_behavioral_fallback():
    # behavioral/__init__.py uses get("breadth_divergence_penalty", 0.0).
    assert DEFAULTS.get("settings.behavioral.breadth_divergence_penalty") == 0.0
