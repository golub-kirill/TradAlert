"""
Behavioral data fetchers.

Fetches behavioral / structural data feeds:
 COT — CFTC Commitments of Traders report
 NAAIM — NAAIM exposure index
 AAII — AAII bull-bear survey
 Breadth — % S&P 500 above MA200
 Sector — (XLI+XLF)/(XLP+XLU) rotation ratio
 Form 4 — SEC EDGAR Form 4 insider transactions (per-ticker)
 Short Int — yfinance shortPercentOfFloat (per-ticker)

Each fetcher follows the fail-open convention: stale data > 14 days is
treated as missing; missing axes are dropped from the composite.

Public API
----------
fetch_all_behavioral(settings) -> dict[str, pd.DataFrame | dict]
fetch_cot(contract) -> pd.DataFrame
fetch_naaim -> pd.DataFrame
fetch_aaii -> pd.DataFrame
compute_sp500_breadth -> pd.DataFrame
compute_sector_rotation -> pd.DataFrame
fetch_form4(ticker) -> dict
fetch_short_interest(ticker) -> dict
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.fetchers.behavioral.aaii import fetch_aaii
from core.fetchers.behavioral.breadth import compute_sp500_breadth, compute_sector_rotation
from core.fetchers.behavioral.cot import fetch_cot, fetch_all_cot
from core.fetchers.behavioral.form4 import fetch_form4
from core.fetchers.behavioral.naaim import fetch_naaim
from core.fetchers.behavioral.short_interest import fetch_short_interest

logger = logging.getLogger(__name__)

__all__ = [
    "fetch_all_behavioral",
    "fetch_cot",
    "fetch_all_cot",
    "fetch_naaim",
    "fetch_aaii",
    "compute_sp500_breadth",
    "compute_sector_rotation",
    "fetch_form4",
    "fetch_short_interest",
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
    aaii — AAII DataFrame
    breadth — S&P 500 breadth DataFrame
    sector_rotation — sector ratio DataFrame
    Per-ticker data (form4, short_interest) is fetched on demand.

    ``behavioral.data_dir`` and ``behavioral.stale_window_days``
    from settings.yaml are now read here and forwarded to every sub-fetcher
    (previously each fetcher hardcoded its own value, so the YAML keys were
    dead).
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

    # NAAIM
    try:
        naaim = fetch_naaim(
            data_dir=data_dir, staleness_days=staleness_days, force=force,
        )
        if not naaim.empty:
            result["naaim"] = naaim
    except Exception as exc:
        logger.warning("[behavioral] NAAIM fetch failed: %s", exc, exc_info=True)
        failed.append("naaim")

    # AAII
    try:
        aaii = fetch_aaii(
            data_dir=data_dir, staleness_days=staleness_days, force=force,
        )
        if not aaii.empty:
            result["aaii"] = aaii
    except Exception as exc:
        logger.warning("[behavioral] AAII fetch failed: %s", exc, exc_info=True)
        failed.append("aaii")

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
    # health snapshot without grepping the 5 per-axis WARNs.
    actual = len(result)
    if failed:
        logger.warning("[behavioral] %d feeds fetched, %d axes failed: %s",
                       actual, len(failed), failed)
    else:
        logger.info("[behavioral] %d feeds fetched, 0 axes failed", actual)
    return result
