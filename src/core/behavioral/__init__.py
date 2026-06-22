"""
Behavioral regime classifier.

Computes a ``BehavioralState`` from fetched behavioral data feeds:
 - breadth → breadth_state, breadth_divergence
 - sector rotation → sector_cycle
 - COT (leveraged-money net) → positioning_state

The sentiment axis (AAII, then CNN Fear & Greed) was PURGED: AAII gated its free feed
and F&G has no history before its ~2011 inception (so ~32% of the 2000-2026 backtest
ran sentiment-absent) and overlaps the VIX/breadth axes. Behavioral score is 3-axis.

The composite ``behavioral_score ∈ [0, 1]`` drives the position size
multiplier together with the macro ``risk_on_score`` (geometric mean).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


def _column_or_warn(df, col, axis):
    """Return ``df[col]`` if present, else log a LOUD warning and return None.

    A non-empty feed that lacks its expected column means the producer's schema
    drifted (stale cache or feed-format change). We refuse to crash the whole
    run, but the mismatch is VISIBLE and must be fixed at the source — it is
    never silently swallowed.
    """
    if df is not None and col in getattr(df, "columns", ()):
        return df[col]
    logger.warning(
        "behavioral '%s' axis DISABLED — expected column %r missing from a "
        "non-empty feed (producer/consumer schema mismatch — FIX the fetcher). "
        "Got columns=%s",
        axis, col, list(getattr(df, "columns", [])) if df is not None else None,
    )
    return None


# Publication lag (calendar days) from each feed's data date to its public
# release. Backtest as_of slicing must use the RELEASE date, not the report/
# survey date, or a print leaks into decisions made before it existed:
#   cot_es : CFTC TFF reports Tuesday positions, released the following Friday (+3).
# Price-derived feeds (breadth, sector_rotation) have no publication lag.
# (NAAIM purged 2026-06-18 — positioning is COT-only.)
_RELEASE_LAG_DAYS = {"cot_es": 3}


def _release_align(df: "pd.DataFrame | None", lag_days: int) -> "pd.DataFrame | None":
    """Return a copy of ``df`` with its DatetimeIndex shifted forward by
    ``lag_days`` so as_of slicing reflects when the feed was RELEASED, not its
    report/survey date. Input is never mutated; non-datetime indexes pass through.
    """
    if df is None or getattr(df, "empty", True) or lag_days <= 0:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    out = df.copy()
    out.index = out.index + pd.Timedelta(days=lag_days)
    return out


@dataclass
class BehavioralState:
    """
    Market-wide behavioral regime classification.

    Attributes
    ----------
    breadth_state : "STRONG" | "NEUTRAL" | "DETERIORATING" | "WEAK"
    breadth_divergence : True when SPY at new high but breadth < 55%
    sector_cycle : "EARLY" | "MID" | "LATE" | "DEFENSIVE_LEAD"
    positioning_state : "CROWDED_LONG" | "NEUTRAL" | "CROWDED_SHORT"

    behavioral_score : composite [0, 1]
    confidence : fraction of axes with fresh data
    missing_axes : which axes were unavailable
    """
    breadth_state: str
    breadth_divergence: bool
    sector_cycle: str
    positioning_state: str

    behavioral_score: float
    confidence: float
    missing_axes: list[str] = field(default_factory=list)
    # derived position-size multiplier — set by classify_behavioral_state.
    size_multiplier: float = 1.0


# Cache keyed by (as_of, data fingerprint, settings): run_prepped recomputes every
# bar despite identical inputs (most behavioral feeds are weekly/monthly).
_BEHAV_STATE_CACHE: dict[tuple, "BehavioralState"] = {}
_BEHAV_STATE_CACHE_MAX: int = 4096


def _behav_fingerprint(data: dict, spy_df, as_of) -> tuple:
    asof_key = as_of.date() if hasattr(as_of, "date") else as_of
    sigs = []
    for k in sorted(data):
        v = data[k]
        if isinstance(v, pd.DataFrame):
            last_idx = v.index[-1] if not v.empty else None
            sigs.append((k, len(v), last_idx))
        elif isinstance(v, dict):
            sigs.append((k, "dict", len(v)))
        else:
            sigs.append((k, type(v).__name__))
    spy_sig = None
    if spy_df is not None and not spy_df.empty:
        spy_sig = (len(spy_df), spy_df.index[-1])
    return (asof_key, tuple(sigs), spy_sig)


def classify_behavioral_state(
        data: dict,
        settings: dict | None = None,
        spy_df: pd.DataFrame | None = None,
        as_of: pd.Timestamp | None = None,
) -> BehavioralState:
    """
    Classify the behavioral regime from fetched data.

    Parameters
    ----------
    data : Output of ``fetch_all_behavioral``.
    settings : Full settings dict (reads ``behavioral.*`` keys).
    spy_df : SPY OHLCV for breadth divergence detection.
    as_of : Point-in-time slice. None → latest.

    Returns
    -------
    BehavioralState
    """
    # cache lookup keyed by (as_of, data fingerprint, behavioral
    # settings sub-dict).
    behav_settings_key = None
    if settings:
        bcfg_seen = (settings.get("behavioral", {}) or {})
        behav_settings_key = tuple(sorted(((k, str(v)) for k, v in bcfg_seen.items()),
                                          key=lambda kv: kv[0]))
    try:
        cache_key = (_behav_fingerprint(data, spy_df, as_of), behav_settings_key)
    except Exception:
        cache_key = None
    if cache_key is not None and cache_key in _BEHAV_STATE_CACHE:
        return _BEHAV_STATE_CACHE[cache_key]

    behavioral_cfg = (settings or {}).get("behavioral", {})

    def _slice(df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or df.empty:
            return None
        if as_of is not None:
            try:
                df = df.loc[:as_of]
            except TypeError as exc:
                # Treat the axis as missing rather than skipping the as_of slice:
                # skipping would expose the full series (future data) → look-ahead.
                logger.warning(
                    "behavioral._slice: as_of=%s slice failed on %s — "
                    "treating axis as MISSING (refusing to use full-history "
                    "data to avoid look-ahead): %s",
                    as_of, type(df.index).__name__, exc, exc_info=True,
                )
                return None
        return df if not df.empty else None

    breadth_df = _slice(data.get("breadth"))
    sector_df = _slice(data.get("sector_rotation"))
    # Release-align the survey/report feeds before slicing so a print is only
    # visible from its publication date onward (no look-ahead in backtests).
    cot_es = _slice(_release_align(data.get("cot_es"), _RELEASE_LAG_DAYS["cot_es"]))

    spy_t = _slice(spy_df) if spy_df is not None else None

    missing_axes: list[str] = []

    # ── breadth_state ─────────────────────────────────────────────────────
    if breadth_df is not None and not breadth_df.empty:
        breadth_state, breadth_divergence = _classify_breadth(
            breadth_df, spy_t)
    else:
        breadth_state = "NEUTRAL"
        breadth_divergence = False
        missing_axes.append("breadth_state")

    # ── sector_cycle ─────────────────────────────────────────────────────
    if sector_df is not None and not sector_df.empty:
        sector_cycle = _classify_sector_cycle(sector_df)
    else:
        sector_cycle = "MID"
        missing_axes.append("sector_cycle")

    # ── positioning_state ────────────────────────────────────────────────
    # COT-only positioning (CFTC leveraged-money net). NAAIM purged 2026-06-18 —
    # its free feed sunset 2026-08-01 and its A/B contribution was ≈0 (−0.72R).
    # Axis missing only when COT is absent.
    cot_ok = cot_es is not None and not cot_es.empty
    if cot_ok:
        positioning_state = _classify_positioning(cot_es)
    else:
        positioning_state = "NEUTRAL"
        missing_axes.append("positioning_state")

    # ── composite behavioral_score ───────────────────────────────────────
    state_values = {
        "breadth_state": breadth_state,
        "sector_cycle": sector_cycle,
        "positioning_state": positioning_state,
    }

    behavioral_weights = behavioral_cfg.get("behavioral_weights", {})
    axis_weights = behavioral_cfg.get("axis_weights", {
        "breadth_state": 4,
        "sector_cycle": 2,
        "positioning_state": 2,
    })

    numerator = 0.0
    denominator = 0.0
    for axis, weight in axis_weights.items():
        if axis in missing_axes:
            continue
        state_label = state_values.get(axis, "NEUTRAL")
        weight_map = behavioral_weights.get(axis, {})
        score = weight_map.get(state_label, 0.5)
        numerator += score * weight
        denominator += weight

    behavioral_score = numerator / denominator if denominator > 0 else 0.5
    total_axes = len(axis_weights)
    # Clamp ≥ 0: missing_axes uses canonical names (one entry per axis) so it can't
    # exceed total_axes, but guard against negative confidence anyway.
    confidence = max(0.0, (total_axes - len(missing_axes)) / total_axes) if total_axes > 0 else 0.0

    # derive size_multiplier from behavioral_score using behavioral
    # floor/ceiling. Apply breadth_divergence_penalty before mapping so a
    # divergence visibly cuts size.
    divergence_penalty = float(behavioral_cfg.get("breadth_divergence_penalty", 0.0))
    adjusted_score = behavioral_score
    if breadth_divergence:
        adjusted_score = max(0.0, behavioral_score - divergence_penalty)

    floor = float(behavioral_cfg.get("size_mult_floor", 0.25))
    ceiling = float(behavioral_cfg.get("size_mult_ceiling", 1.0))
    size_multiplier = floor + (ceiling - floor) * adjusted_score

    state = BehavioralState(
        breadth_state=breadth_state,
        breadth_divergence=breadth_divergence,
        sector_cycle=sector_cycle,
        positioning_state=positioning_state,
        behavioral_score=round(behavioral_score, 4),
        confidence=round(confidence, 4),
        missing_axes=missing_axes,
        size_multiplier=round(size_multiplier, 4),
    )
    # cache write with simple cap eviction.
    if cache_key is not None:
        if len(_BEHAV_STATE_CACHE) >= _BEHAV_STATE_CACHE_MAX:
            _BEHAV_STATE_CACHE.clear()
        _BEHAV_STATE_CACHE[cache_key] = state
    return state


def _classify_breadth(
        breadth_df: pd.DataFrame,
        spy_df: pd.DataFrame | None = None,
) -> tuple[str, bool]:
    """
    Classify breadth state and detect divergence.

    breadth_state:
    > 70 → STRONG
    50–70 → NEUTRAL
    30–50 and falling → DETERIORATING
    < 30 → WEAK

    breadth_divergence:
    SPY making 20d new high AND breadth < 55% → True
    """
    _pct_col = _column_or_warn(breadth_df, "pct_above_ma200", "breadth")
    if _pct_col is None:
        return "NEUTRAL", False
    pct = float(_pct_col.iloc[-1])

    # Determine trend direction (last 5 bars)
    if len(breadth_df) >= 5:
        recent = _pct_col.iloc[-5:].values
        falling = recent[-1] < recent[0]
    else:
        falling = False

    if pct > 70:
        state = "STRONG"
    elif pct >= 50:
        state = "NEUTRAL"
    elif pct >= 30 and falling:
        state = "DETERIORATING"
    elif pct < 30:
        state = "WEAK"
    else:
        state = "NEUTRAL"

    # Divergence detection
    divergence = False
    if spy_df is not None and not spy_df.empty and len(spy_df) >= 20:
        spy_high_20d = float(spy_df["high"].iloc[-20:].max())
        spy_latest = float(spy_df["close"].iloc[-1])
        if spy_latest >= spy_high_20d and pct < 55:
            divergence = True

    return state, divergence


def _classify_sector_cycle(sector_df: pd.DataFrame) -> str:
    """
    Classify sector cycle from (XLI+XLF)/(XLP+XLU) ratio.

    Ratio rising 60d → EARLY
    Stable → MID
    Falling → LATE
    Defensive strongly outperforming → DEFENSIVE_LEAD
    """
    if len(sector_df) < 60:
        return "MID"

    normalized = _column_or_warn(sector_df, "normalized", "sector_cycle")
    if normalized is None:
        return "MID"
    current = float(normalized.iloc[-1])
    ago_20 = float(normalized.iloc[-20])
    ago_60 = float(normalized.iloc[-60])

    delta_20 = current - ago_20
    delta_60 = current - ago_60

    if delta_60 > 0.05 and delta_20 > 0:
        return "EARLY"
    elif delta_60 < -0.05 and delta_20 < 0:
        if current < 0.95:
            return "DEFENSIVE_LEAD"
        return "LATE"
    else:
        return "MID"


def _classify_positioning(cot_es: pd.DataFrame) -> str:
    """
    Classify positioning from COT leveraged-money net positioning (percentile).

    > 90th → CROWDED_LONG
    < 10th → CROWDED_SHORT
    else → NEUTRAL

    (NAAIM purged 2026-06-18 — COT is the sole positioning source.)
    """
    # COT: leveraged-money net positioning percentile (TFF report → ``lev_net``).
    # Guarded: a missing/empty feed degrades this axis to NEUTRAL, never raises.
    _lev = _column_or_warn(cot_es, "lev_net", "positioning(COT)")
    cot_pctile = _rolling_percentile(_lev, 260) if _lev is not None else None
    if cot_pctile is None:
        return "NEUTRAL"

    if cot_pctile > 90:
        return "CROWDED_LONG"
    elif cot_pctile < 10:
        return "CROWDED_SHORT"
    else:
        return "NEUTRAL"


def _rolling_percentile(series: pd.Series, lookback: int) -> float | None:
    """
    Compute the percentile rank of the latest value within a rolling window.

    Returns a value in [0, 100], or None if insufficient data.
    """
    if len(series) < lookback:
        window = series
    else:
        window = series.iloc[-lookback:]

    latest = window.iloc[-1]
    if pd.isna(latest):
        return None

    count_below = (window < latest).sum()
    total = len(window.dropna())
    if total == 0:
        return None

    return (count_below / total) * 100
