"""Historical base rates for the advisor — the empirical edge of a setup so the
critic anchors its verdict on "materially worse than this" rather than a vibe.

The table is precomputed offline from resolved backtest trades
(``scripts/studies/build_advisor_base_rates.py``) and stored at
``data/advisor_base_rates.json``. It holds aggregates only — win-rate, mean R,
and count per setup cell — never per-trade rows, so the table the live advisor
reads cannot leak the outcome of the trade under review (look-ahead safe).

Fail-open: a missing or corrupt file yields ``{}`` and the advisor simply runs
without the edge line.

Keys: ``"signal_type"`` | ``"signal_type|REGIME"`` | ``"signal_type|REGIME|TREND"``
plus ``"__all__"``. Each value: ``{n, win_rate, avg_r}`` (mean R is the per-trade
expectancy for an R-multiple).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

__all__ = ["load_base_rates", "lookup", "format_base_rate"]

# A cell thinner than this is too noisy to anchor on — fall back to a coarser key.
_MIN_N = 20


def load_base_rates(path=None) -> dict:
    """Load the base-rate table (``data/advisor_base_rates.json``). ``{}`` on any
    failure — missing file, bad JSON — so the advisor runs without it."""
    from core.paths import DATA_DIR

    p = path or (DATA_DIR / "advisor_base_rates.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # missing/corrupt is non-fatal
        return {}


def _key(signal_type, regime=None, trend=None) -> str:
    parts = [str(signal_type or "").lower()]
    if regime:
        parts.append(str(regime).upper())
    if trend:
        parts.append(str(trend).upper())
    return "|".join(parts)


def lookup(table: dict, signal_type: str, regime: str | None = None,
           trend: str | None = None, *, min_n: int = _MIN_N) -> dict:
    """Most specific cell with ``n >= min_n``, walking coarser on a thin/missing
    cell: ``sig|regime|trend`` → ``sig|regime`` → ``sig`` → ``__all__``. Returns
    the matched cell plus its ``key``, or ``{}`` when the table is empty."""
    if not table:
        return {}
    for k in (_key(signal_type, regime, trend),
              _key(signal_type, regime),
              _key(signal_type)):
        cell = table.get(k)
        if cell and cell.get("n", 0) >= min_n:
            return {**cell, "key": k}
    # Coarsest fallbacks: the global cell, then a thin signal-type cell as a last
    # resort so a rare setup still gets *some* anchor rather than none.
    glob = table.get("__all__")
    if glob:
        return {**glob, "key": "__all__"}
    cell = table.get(_key(signal_type))
    return {**cell, "key": _key(signal_type)} if cell else {}


def format_base_rate(cell: dict) -> str:
    """One-line summary for the prompt/-v, or ``""`` when unknown."""
    if not cell:
        return ""
    wr = cell.get("win_rate")
    ar = cell.get("avg_r")
    if wr is None and ar is None:
        return ""
    parts = []
    if wr is not None:
        parts.append(f"{wr:.0%} win")
    if ar is not None:
        parts.append(f"{ar:+.2f}R avg (expectancy)")
    return f"{'; '.join(parts)}; n={int(cell.get('n', 0))}"
