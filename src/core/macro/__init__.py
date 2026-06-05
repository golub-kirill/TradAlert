"""
Macro regime classification.

Public API
----------
MacroState — dataclass with per-axis labels + risk_on_score
classify_macro_state — build MacroState from macro series
"""

from core.macro.regime import MacroState, classify_macro_state

__all__ = ["MacroState", "classify_macro_state"]
