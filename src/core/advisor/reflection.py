"""Reflection — the advisor's own recent calibration, fed back to the judge.

Answers "when this advisor said disagree, how often was it actually right, and
what did those trades really do?" so the critic can correct a miscalibrated bias
(the over-eager 'disagree' this program started from) instead of repeating it.

The table is precomputed offline from resolved live verdicts
(``scripts/studies/build_advisor_calibration.py``: parse the journaled verdict in
``scan_results.advisor_note`` → replay the signal → bucket by verdict) and stored
at ``data/advisor_calibration.json``. Aggregates only — no per-trade rows — so it
stays look-ahead safe. Fail-open: a missing/thin/corrupt file yields ``""`` and
no calibration line is added, so today's prompts are unchanged until live
verdicts accrue.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

__all__ = ["load_reflection", "format_reflection"]

# Below this many resolved calls the calibration is noise — emit nothing.
_MIN_N = 15


def load_reflection(path=None) -> dict:
    """Load the calibration table (``data/advisor_calibration.json``). ``{}`` on
    any failure so the advisor runs without a calibration line."""
    from core.paths import DATA_DIR

    p = path or (DATA_DIR / "advisor_calibration.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # missing/corrupt is non-fatal
        return {}


def format_reflection(table: dict) -> str:
    """One-line calibration summary for the judge, or ``""`` when too thin."""
    if not table:
        return ""
    n = int(table.get("n", 0))
    by_verdict = table.get("by_verdict") or {}
    if n < _MIN_N or not by_verdict:
        return ""
    parts: list[str] = []
    for label in ("agree", "disagree", "flag"):
        cell = by_verdict.get(label) or {}
        cn = int(cell.get("n", 0))
        if cn <= 0:
            continue
        seg = f"{label} {cell.get('correct', 0):.0%} right (n={cn}"
        avg_r = cell.get("avg_r")
        if avg_r is not None:
            seg += f", {avg_r:+.2f}R avg"
        seg += ")"
        parts.append(seg)
    if not parts:
        return ""
    return (
        f"Recent live advisor calibration over {n} resolved calls — "
        + "; ".join(parts)
        + ". Weight your own verdict accordingly; do not repeat a miscalibrated bias."
    )
