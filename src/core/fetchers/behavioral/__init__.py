"""
Behavioral data fetchers.

Fetches behavioral / structural data feeds:
 COT — CFTC Commitments of Traders report
 NAAIM — NAAIM exposure index
 Breadth — % S&P 500 above MA200
 Sector — (XLI+XLF)/(XLP+XLU) rotation ratio

(The sentiment axis — AAII, then CNN Fear & Greed — was PURGED; see core.behavioral.)

Each fetcher follows the fail-open convention: stale data > 14 days is
treated as missing; missing axes are dropped from the composite.

Public API
----------
fetch_all_behavioral(settings) -> dict[str, pd.DataFrame | dict]
fetch_cot(contract) -> pd.DataFrame
fetch_naaim -> pd.DataFrame
compute_sp500_breadth -> pd.DataFrame
compute_sector_rotation -> pd.DataFrame
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.fetchers.behavioral.breadth import compute_sp500_breadth, compute_sector_rotation
from core.fetchers.behavioral.cot import fetch_cot, fetch_all_cot
from core.fetchers.behavioral.naaim import fetch_naaim

logger = logging.getLogger(__name__)

__all__ = [
    "fetch_all_behavioral",
    "fetch_cot",
    "fetch_all_cot",
    "fetch_naaim",
    "compute_sp500_breadth",
    "compute_sector_rotation",
]


def fetch_all_behavioral(
        settings_path: str | Path = "config/settings.yaml",
        force: bool = False,
) -> dict:
    """
    Fetch all behavioral data feeds in one call.

    Parameters
    ----------
    settings_path : Path to settings.yaml.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    dict with keys:
    cot_es, cot_tnote, cot_vix — COT DataFrames
    naaim — NAAIM DataFrame
    breadth — S&P 500 breadth DataFrame
    sector_rotation — sector ratio DataFrame

    ``behavioral.data_dir`` and ``behavioral.stale_window_days`` from
    settings.yaml are read here and forwarded to every sub-fetcher.
    """
    # load behavioral config block once and forward to each fetcher.
    from core.defaults import DEFAULTS
    import yaml
    try:
        cfg_root = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[behavioral] settings.yaml unreadable (%s); using defaults", exc)
        cfg_root = {}
    bcfg = (cfg_root or {}).get("behavioral", {}) or {}
    data_dir = bcfg.get("data_dir", DEFAULTS.get("settings.behavioral.data_dir"))
    staleness_days = int(bcfg.get(
        "stale_window_days", DEFAULTS.get("settings.behavioral.stale_window_days"),
    ))

    result: dict = {}
    failed: list[str] = []  # track failures for summary

    # COT data
    try:
        cot_data = fetch_all_cot(
            data_dir=data_dir, staleness_days=staleness_days, force=force,
        )
        result.update({f"cot_{k}": v for k, v in cot_data.items()})
    except Exception as exc:
        logger.warning("[behavioral] COT fetch failed: %s", exc, exc_info=True)
        failed.append("cot")

    # NAAIM — PURGED 2026-06-18. Positioning is COT-only (settings.behavioral.use_naaim:
    # false; A/B cost was -0.72R ≈ 0). The free feed sunsets 2026-08-01 anyway, so the
    # live scan no longer fetches it. (naaim.py + the classifier toggle remain for now;
    # full code removal is a follow-up tidy.)

    # Breadth (compute_sp500_breadth uses staleness_hours; convert from days)
    try:
        breadth = compute_sp500_breadth(
            data_dir=data_dir, staleness_hours=staleness_days * 24, force=force,
        )
        if not breadth.empty:
            result["breadth"] = breadth
    except Exception as exc:
        logger.warning("[behavioral] breadth computation failed: %s", exc, exc_info=True)
        failed.append("breadth")

    # Sector rotation (compute_sector_rotation uses staleness_hours)
    try:
        sector = compute_sector_rotation(
            data_dir=data_dir, staleness_hours=staleness_days * 24, force=force,
        )
        if not sector.empty:
            result["sector_rotation"] = sector
    except Exception as exc:
        logger.warning("[behavioral] sector rotation computation failed: %s", exc, exc_info=True)
        failed.append("sector_rotation")

    # aggregate "X/Y fetched, Z failed: …" line so operators see the
    # health snapshot without grepping the per-axis WARNs.
    actual = len(result)
    if failed:
        logger.warning("[behavioral] %d feeds fetched, %d axes failed: %s",
                       actual, len(failed), failed)
    else:
        logger.info("[behavioral] %d feeds fetched, 0 axes failed", actual)
    return result
