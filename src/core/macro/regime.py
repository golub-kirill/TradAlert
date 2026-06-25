"""
Macro regime classifier.

Computes a ``MacroState`` from the fetched macro series. Each axis is
classified into a discrete state, then mapped to a [0, 1] value and
weight-averaged into a composite ``risk_on_score``.

Public API
----------
classify_macro_state(series, as_of, settings) -> MacroState
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

logger = logging.getLogger(__name__)

# ── type aliases ──────────────────────────────────────────────────────────────

PolicyStance = Literal["HIKING", "HOLD", "CUTTING"]
CurveState = Literal["INVERTED", "FLAT", "NORMAL", "STEEPENING_FROM_INVERTED"]
CreditState = Literal["TIGHT", "NORMAL", "WIDE", "WIDENING"]
LiquidityTrend = Literal["EXPANDING", "FLAT", "CONTRACTING"]
InflationState = Literal["ACCELERATING", "STABLE", "DECELERATING"]
RealYieldState = Literal["HIGH_POSITIVE", "NORMAL", "NEGATIVE"]
DollarState = Literal["STRONG_RISING", "STABLE", "WEAK_FALLING"]
OilState = Literal["RISING", "STABLE", "FALLING"]
WcsSpreadState = Literal["WIDE", "NORMAL"]
EarningsBreadth = Literal["IMPROVING", "STABLE", "DETERIORATING"]


# ── dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class MacroState:
    """Per-axis macro regime classification + composite score."""
    policy_stance_us: PolicyStance = "HOLD"
    curve_state: CurveState = "NORMAL"
    credit_state: CreditState = "NORMAL"
    liquidity_trend: LiquidityTrend = "FLAT"
    inflation_state: InflationState = "STABLE"
    real_yield_state: RealYieldState = "NORMAL"
    dollar_state: DollarState = "STABLE"
    oil_state: OilState = "STABLE"
    wcs_spread_state: WcsSpreadState = "NORMAL"
    earnings_breadth: EarningsBreadth = "STABLE"

    risk_on_score: float = 0.5  # neutral by default
    confidence: float = 1.0
    missing_axes: list[str] = field(default_factory=list)
    # derived position-size multiplier — set by classify_macro_state.
    size_multiplier: float = 1.0


# ── axis value mappings ──────────────────────────────────────────────────────

_RISK_ON_VALUES = {
    "policy_stance_us": {"HIKING": 0.0, "HOLD": 0.5, "CUTTING": 1.0},
    "curve_state": {"INVERTED": 0.3, "STEEPENING_FROM_INVERTED": 0.0, "FLAT": 0.6, "NORMAL": 1.0},
    "credit_state": {"WIDE": 0.0, "WIDENING": 0.2, "NORMAL": 0.7, "TIGHT": 1.0},
    "liquidity_trend": {"CONTRACTING": 0.2, "FLAT": 0.6, "EXPANDING": 1.0},
    "inflation_state": {"ACCELERATING": 0.4, "STABLE": 0.7, "DECELERATING": 1.0},
    "real_yield_state": {"HIGH_POSITIVE": 0.4, "NORMAL": 0.8, "NEGATIVE": 1.0},
    "dollar_state": {"STRONG_RISING": 0.4, "STABLE": 0.8, "WEAK_FALLING": 1.0},
    "oil_state": {"RISING": 0.5, "STABLE": 0.7, "FALLING": 0.8},
    "wcs_spread_state": {"WIDE": 0.3, "NORMAL": 0.7},
    "earnings_breadth": {"DETERIORATING": 0.2, "STABLE": 0.7, "IMPROVING": 1.0},
}

_DEFAULT_AXIS_WEIGHTS = {
    "policy_stance_us": 3,
    "curve_state": 4,
    "credit_state": 4,
    "liquidity_trend": 3,
    "inflation_state": 2,
    "real_yield_state": 2,
    "dollar_state": 1,
    "oil_state": 1,
    "earnings_breadth": 2,
}

# FRED monthly series are dated at the reference month start but published the
# following month. Treat a monthly print for month M as KNOWN only from the start
# of M+2 — conservative (never before CPI/PCE/FEDFUNDS actually release), so a
# backtest replaying a past as_of cannot see an unpublished figure. Daily/weekly
# series publish same/next day and need no shift.
_MONTHLY_FRED = frozenset({"PCEPILFE", "CPIAUCSL", "CPILFESL", "CPIAUCNS", "FEDFUNDS"})
_MONTHLY_PUBLISH_LAG = pd.DateOffset(months=2)

# classify_macro_state runs once per bar in the portfolio backtester, but the
# classified state changes only ~20×/yr (most inputs are monthly/weekly). Cache
# results keyed by (as_of-date, series fingerprint).
_MACRO_STATE_CACHE: dict[tuple, "MacroState"] = {}
_MACRO_STATE_CACHE_MAX: int = 4096  # safety bound (LRU-ish via clear)


def _macro_fingerprint(series: dict[str, "pd.DataFrame"], as_of) -> tuple:
    """Fingerprint inputs for cache keying — invalidates if series shape changes."""
    asof_key = as_of.date() if hasattr(as_of, "date") else as_of
    sigs = []
    for sid in sorted(series):
        df = series[sid]
        last_idx = df.index[-1] if (df is not None and not df.empty) else None
        sigs.append((sid, len(df) if df is not None else 0, last_idx))
    return (asof_key, tuple(sigs))


def classify_macro_state(
        series: dict[str, pd.DataFrame],
        as_of: pd.Timestamp | None = None,
        settings: dict | None = None,
) -> MacroState:
    """
    Classify the macro regime from a dict of macro series.

    Parameters
    ----------
    series : series_id → DataFrame (with ``value`` column).
    as_of : Point-in-time slice. None → latest.
    settings : Optional settings dict for custom axis weights.

    Returns
    -------
    MacroState with per-axis labels and composite risk_on_score.
    """
    # cache hit on (as_of, fingerprint of series shape + settings.macro).
    # Cache is per-process; safe for sweep workers (each gets its own).
    macro_settings_key = None
    if settings:
        macro_settings_key = tuple(sorted(((k, str(v)) for k, v in
                                           (settings.get("macro", {}) or {}).items()),
                                          key=lambda kv: kv[0]))
    try:
        cache_key = (_macro_fingerprint(series, as_of), macro_settings_key)
    except Exception:
        cache_key = None

    if cache_key is not None and cache_key in _MACRO_STATE_CACHE:
        return _MACRO_STATE_CACHE[cache_key]

    state = MacroState()
    missing: list[str] = []

    # Slice each series to as_of, pushing monthly FRED prints back to their
    # release date so an unpublished figure can't leak into a past decision.
    def _eff_asof(sid: str):
        if as_of is None:
            return None
        return (as_of - _MONTHLY_PUBLISH_LAG) if sid in _MONTHLY_FRED else as_of

    def _val(sid: str) -> float | None:
        df = series.get(sid)
        if df is None or df.empty:
            return None
        s = df["value"]
        if as_of is not None:
            s = s.loc[:_eff_asof(sid)]
        if s.empty:
            return None
        v = s.iloc[-1]
        return float(v) if not pd.isna(v) else None

    def _series(sid: str) -> pd.Series | None:
        df = series.get(sid)
        if df is None or df.empty:
            return None
        s = df["value"]
        if as_of is not None:
            s = s.loc[:_eff_asof(sid)]
        return s if not s.empty else None

    def _val_ago(sid: str, months: int) -> float | None:
        """Get value from `months` months ago, frequency-agnostic."""
        s = _series(sid)
        if s is None or len(s) < 2:
            return None
        target = (as_of or s.index[-1]) - pd.DateOffset(months=months)
        masked = s.loc[s.index <= target]
        return float(masked.iloc[-1]) if not masked.empty else None

    def _series_ago(sid: str, months: int) -> pd.Series | None:
        """Get series truncated to `months` months ago, frequency-agnostic."""
        s = _series(sid)
        if s is None or len(s) < 2:
            return None
        cutoff = (as_of or s.index[-1]) - pd.DateOffset(months=months)
        return s.loc[s.index > cutoff] if not s.empty else None

    # Policy stance US (Fed Funds) — 6-month delta, frequency-agnostic
    ff = _series("FEDFUNDS")
    if ff is not None and len(ff) >= 2:
        current = ff.iloc[-1]
        ago_6m = _val_ago("FEDFUNDS", 6)
        if ago_6m is not None:
            delta = current - ago_6m
            if delta > 0.25:
                state.policy_stance_us = "HIKING"
            elif delta < -0.25:
                state.policy_stance_us = "CUTTING"
            else:
                state.policy_stance_us = "HOLD"
        else:
            missing.append("policy_stance_us")
    else:
        missing.append("policy_stance_us")

    # Curve state (10y - 3m spread)
    dgs10 = _val("DGS10")
    dgs3mo = _val("DGS3MO")
    if dgs10 is not None and dgs3mo is not None:
        spread = dgs10 - dgs3mo
        # 2-month lookback for "was inverted" check, frequency-agnostic
        dgs10_ago = _val_ago("DGS10", 2)
        dgs3mo_ago = _val_ago("DGS3MO", 2)
        was_inverted = False
        if dgs10_ago is not None and dgs3mo_ago is not None:
            was_spread = dgs10_ago - dgs3mo_ago
            was_inverted = was_spread < 0

        if spread < 0:
            # Inverted now, fresh or sustained (gating on was_inverted would
            # misclassify a fresh inversion as FLAT).
            state.curve_state = "INVERTED"
        elif was_inverted:
            state.curve_state = "STEEPENING_FROM_INVERTED"
        elif spread > 0.5:
            state.curve_state = "NORMAL"
        else:
            state.curve_state = "FLAT"
    else:
        missing.append("curve_state")

    # Credit state (HY OAS)
    hy_oas = _series("BAMLH0A0HYM2")
    if hy_oas is not None:
        current = hy_oas.iloc[-1]
        # 5-year window, frequency-agnostic
        hist = _series_ago("BAMLH0A0HYM2", 60)
        # Require ≥ ~1 year of history by date span (HY OAS is daily, so a row
        # count would let a 12-day window compute the percentile band).
        if hist is not None and len(hist) >= 2 and (hist.index[-1] - hist.index[0]).days >= 330:
            pct_80 = hist.quantile(0.8)
            pct_20 = hist.quantile(0.2)
            if current > pct_80:
                state.credit_state = "WIDE"
            elif current < pct_20:
                state.credit_state = "TIGHT"
            else:
                state.credit_state = "NORMAL"
            # Widening check — 1-month delta, frequency-agnostic
            ago_1m = _val_ago("BAMLH0A0HYM2", 1)
            if ago_1m is not None and current > ago_1m:
                if state.credit_state == "NORMAL":
                    state.credit_state = "WIDENING"
        else:
            state.credit_state = "NORMAL"
    else:
        missing.append("credit_state")

    # Liquidity trend (WALCL - TGA - RRP)
    walcl = _series("WALCL")
    tga = _series("WTREGEN")
    rrp = _series("RRPONTSYD")
    if walcl is not None and tga is not None and rrp is not None:
        # Align indices
        common = walcl.index.intersection(tga.index).intersection(rrp.index)
        if len(common) >= 12:
            net_liq = walcl.loc[common] - tga.loc[common] - rrp.loc[common]
            # `common` is unique, but .loc[common] reindexes against each
            # series' own index and resurrects duplicate dates (a revised or
            # double-printed FRED print). Collapse to one row per date so the
            # net_liq.loc[label] lookups below stay scalar — a Series there
            # makes the delta truth-tests raise.
            net_liq = net_liq[~net_liq.index.duplicated(keep="last")]
            # 2-month and 4-month deltas, frequency-agnostic
            val_now = net_liq.iloc[-1]
            # Reconstruct net_liq at those points for accuracy
            idx_2m = net_liq.index[net_liq.index <= (as_of or net_liq.index[-1]) - pd.DateOffset(months=2)]
            idx_4m = net_liq.index[net_liq.index <= (as_of or net_liq.index[-1]) - pd.DateOffset(months=4)]
            if len(idx_2m) > 0 and len(idx_4m) > 0:
                val_2m = net_liq.loc[idx_2m[-1]]
                val_4m = net_liq.loc[idx_4m[-1]]
                delta_2m = val_now - val_2m
                delta_4m = val_2m - val_4m
                # ΔΔ direction
                if delta_2m > 0 and delta_2m > delta_4m:
                    state.liquidity_trend = "EXPANDING"
                elif delta_2m < 0 and delta_2m < delta_4m:
                    state.liquidity_trend = "CONTRACTING"
                else:
                    state.liquidity_trend = "FLAT"
            else:
                state.liquidity_trend = "FLAT"
        else:
            missing.append("liquidity_trend")
    else:
        missing.append("liquidity_trend")

    # Inflation state (Core PCE Y/Y) — time-based, frequency-agnostic
    pce = _series("PCEPILFE")
    pce_12m_ago = _val_ago("PCEPILFE", 12)
    if pce is not None and pce_12m_ago is not None and pce_12m_ago > 0:
        # True 12-month YoY via time-based lookback, so the span matches the
        # yoy_6mo_ago leg below (a positional iloc[-12] would span only 11 months
        # on monthly data and flip the state on the mismatch alone).
        yoy = (pce.iloc[-1] / pce_12m_ago - 1) * 100
        # YoY 6 months ago
        pce_6m_ago = _val_ago("PCEPILFE", 6)
        pce_18m_ago = _val_ago("PCEPILFE", 18)
        if pce_6m_ago is not None and pce_18m_ago is not None and pce_18m_ago > 0:
            yoy_6mo_ago = (pce_6m_ago / pce_18m_ago - 1) * 100
        else:
            yoy_6mo_ago = yoy
        delta = yoy - yoy_6mo_ago
        if delta > 0.30:
            state.inflation_state = "ACCELERATING"
        elif delta < -0.30:
            state.inflation_state = "DECELERATING"
        else:
            state.inflation_state = "STABLE"
    else:
        missing.append("inflation_state")

    # Real yield state (10y TIPS)
    tips = _val("DFII10")
    if tips is not None:
        if tips > 2.0:
            state.real_yield_state = "HIGH_POSITIVE"
        elif tips < 0.0:
            state.real_yield_state = "NEGATIVE"
        else:
            state.real_yield_state = "NORMAL"
    else:
        missing.append("real_yield_state")

    # Dollar state (DXY) — 3-month delta, frequency-agnostic
    dxy = _series("DX-Y.NYB")
    if dxy is not None and len(dxy) >= 2:
        current = dxy.iloc[-1]
        ago_3m = _val_ago("DX-Y.NYB", 3)
        if ago_3m is not None and ago_3m > 0:
            delta_pct = (current / ago_3m - 1) * 100
            if delta_pct > 5:
                state.dollar_state = "STRONG_RISING"
            elif delta_pct < -5:
                state.dollar_state = "WEAK_FALLING"
            else:
                state.dollar_state = "STABLE"
        else:
            missing.append("dollar_state")
    else:
        missing.append("dollar_state")

    # Oil state (WTI) — 2-month delta, frequency-agnostic
    wti = _series("CL=F")
    if wti is not None and len(wti) >= 2:
        current = wti.iloc[-1]
        ago_2m = _val_ago("CL=F", 2)
        if ago_2m is not None and ago_2m > 0:
            delta_pct = (current / ago_2m - 1) * 100
            if delta_pct > 15:
                state.oil_state = "RISING"
            elif delta_pct < -15:
                state.oil_state = "FALLING"
            else:
                state.oil_state = "STABLE"
        else:
            missing.append("oil_state")
    else:
        missing.append("oil_state")

    # WCS spread state (Brent as WCS proxy)
    wcs = _series("BZ=F")
    wti_val = _val("CL=F")
    if wcs is not None and wti_val is not None and not wcs.empty:
        # BZ=F (Brent) is a rough WCS proxy. Brent−WTI is normally a small
        # positive premium; flag an abnormally wide premium (> 10) as WIDE.
        spread = wcs.iloc[-1] - wti_val
        if spread > 10:
            state.wcs_spread_state = "WIDE"
        else:
            state.wcs_spread_state = "NORMAL"
    # WCS is not a missing axis — it's optional

    # Earnings breadth — placeholder; defaults to STABLE until tier_a earnings
    # data feeds actual breadth.
    state.earnings_breadth = "STABLE"

    # ── composite risk_on_score ──────────────────────────────────────────────
    axis_weights = _DEFAULT_AXIS_WEIGHTS
    if settings:
        custom = settings.get("macro", {}).get("axis_weights", {})
        if custom:
            axis_weights = {k: int(v) for k, v in custom.items()}

    # settings.yaml::macro.risk_on_weights, when present, overrides
    # the per-state values for any axis the user wants to retune. Hardcoded
    # _RISK_ON_VALUES is the fallback.
    risk_values = dict(_RISK_ON_VALUES)
    if settings:
        user_values = (settings.get("macro", {}) or {}).get("risk_on_weights", {}) or {}
        for axis, axis_map in user_values.items():
            if not isinstance(axis_map, dict):
                continue
            merged = dict(risk_values.get(axis, {}))
            merged.update({k: float(v) for k, v in axis_map.items()})
            risk_values[axis] = merged

    total_weight = 0.0
    weighted_sum = 0.0
    for axis_name, weight in axis_weights.items():
        if axis_name in missing:
            continue
        state_val = getattr(state, axis_name)
        risk_val = risk_values.get(axis_name, {}).get(state_val, 0.5)
        weighted_sum += risk_val * weight
        total_weight += weight

    if total_weight > 0:
        state.risk_on_score = weighted_sum / total_weight
    else:
        state.risk_on_score = 0.5

    state.confidence = 1.0 - len(missing) / len(axis_weights)
    state.missing_axes = missing

    # derive size_multiplier from risk_on_score, scaled by configured
    # floor/ceiling. risk_on_score in [0, 1] linearly maps to [floor, ceiling].
    from core.defaults import DEFAULTS as _D
    macro_cfg = (settings or {}).get("macro", {})
    floor = float(macro_cfg.get("size_mult_floor", _D.get("settings.macro.size_mult_floor")))
    ceiling = float(macro_cfg.get("size_mult_ceiling", _D.get("settings.macro.size_mult_ceiling")))
    state.size_multiplier = floor + (ceiling - floor) * state.risk_on_score

    logger.info(
        "[macro] risk_on=%.2f confidence=%.0f%% size_mult=%.2f missing=%s",
        state.risk_on_score, state.confidence * 100, state.size_multiplier, missing,
    )
    # cache write, with simple cap eviction.
    if cache_key is not None:
        if len(_MACRO_STATE_CACHE) >= _MACRO_STATE_CACHE_MAX:
            _MACRO_STATE_CACHE.clear()
        _MACRO_STATE_CACHE[cache_key] = state
    return state
