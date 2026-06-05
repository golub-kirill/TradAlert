"""
Confidence scoring for entry and exit signals.

Enriches a SignalResult in-place after FilterEngine fires. Weights and
threshold dataclasses are loaded from settings.yaml.

Entry sub-scores  (each in [0, 1], weighted-averaged to [0, 100])
    trend_up           close > MA50 > MA200 stack alignment
    ma50_slope         MA50 rising or falling over last 20 bars
    ma200_slope        MA200 rising or falling over last 20 bars
    volume_spike       today's volume vs 20-day average
    rsi_healthy        RSI proximity to the ideal trend-confirm band
    breakout_20d       close vs prior 20-bar high
    near_52w_high      proximity to trailing 252-bar high
    far_from_52w_low   advance above trailing 252-bar low
    macd_bullish       histogram sign and direction
    no_earnings_risk   days until next earnings vs the configured buffer
    relative_strength  ticker outperforming SPY over 20d and 60d
    weekly_trend       higher-timeframe agreement with daily signal
    bb_zscore          Bollinger Z-score positioning

Exit sub-scores
    regime_flip        broad-market trend is no longer BULL
    multi_bar_decay    consecutive negative MACD histogram bars
    rsi_overbought     RSI above the overbought floor
    macd_cross_down    MACD histogram crosses below zero
    vol_expansion      ATR today vs 5-day average
    rs_divergence      ticker underperforming SPY over 20d
    vbp_resistance     overhead high-volume node from Volume-by-Price

Signals where the trigger fired but ``score < min_score_to_alert`` are
marked ``watch_only=True``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from exceptions import ConfigError

if TYPE_CHECKING:
    from core.filter_engine import MarketRegime, SignalResult
    from core.position_manager import Position

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_TIMEFRAME = "daily"
_DEFAULT_HOLD_LOW = 10
_DEFAULT_HOLD_HIGH = 15
_DEFAULT_MIN_SCORE = 60

# Sub-score shape constants (algorithm internals, not config-backed).
# RS-exit: a 20-day relative-strength shortfall of -1/scale (i.e. ~-10%) vs SPY
# saturates the exit score at 1.0.
_RS_EXIT_SCALE = 10.0
# BB z-score: |Z| reaching this half-width drives the momentum score to 0; the
# mean-reversion score saturates at Z = -(0.5 + half-width).
_BB_Z_HALFWIDTH = 2.0


# ── entry-side scoring thresholds (settings.yaml → scanner.entry_thresholds) ──

@dataclass(frozen=True)
class EntryThresholds:
    """Tunable constants used by entry sub-scores.

    Attributes
    ----------
    rsi_healthy_center : RSI value mapped to score 1.0 for trend-confirm band.
    rsi_healthy_half_w : Half-width of the band; score 0.0 at center ± width.
    ma50_slope_scale   : 20-bar MA50 % slope scaled by (slope + scale) / (2*scale).
    ma200_slope_scale  : 20-bar MA200 % slope scaled by (slope + scale) / (2*scale).
                         MA200 moves slower than MA50; default scale is smaller.
    breakout_band_pct  : Below prior 20d high by this many %% maps linearly to 0.
    volume_spike_ratio : Today's volume / 20d avg at which score saturates to 1.0.
    near_52w_high_pct_band  : Distance below 252-bar high (in %) at which the
                              ``near_52w_high`` score reaches 0.0. The score is
                              1.0 within 5%% of the high and decays linearly to
                              0.0 at this band edge.
    far_from_52w_low_pct_floor : Percentage above the 252-bar low at which
                                 ``far_from_52w_low`` saturates to 1.0.
                                 Below this floor the score scales linearly
                                 from 0.0 (at the low) to 1.0 (at the floor).
    """
    rsi_healthy_center: float = 52.5
    rsi_healthy_half_w: float = 12.5
    ma50_slope_scale: float = 2.0
    ma200_slope_scale: float = 0.5
    breakout_band_pct: float = 3.0
    volume_spike_ratio: float = 2.0
    near_52w_high_pct_band: float = 25.0
    far_from_52w_low_pct_floor: float = 30.0


@dataclass(frozen=True)
class ExitThresholds:
    """Tunable constants used by exit sub-scores.

    Attributes
    ----------
    rsi_overbought_floor : RSI value at which rsi_overbought score becomes > 0.
    rsi_overbought_range : RSI delta over the floor at which score saturates to 1.0.
    multi_bar_decay_max  : Consecutive negative macd_hist bars saturating score at 1.0.
    vol_expansion_ratio  : ATR(today)/ATR(5d avg) − 1 saturating score at 1.0.
    vbp_near_atr_mult    : Distance (in ATR units) to overhead VBP node at which
                           ``vbp_resistance`` score is 1.0.
    vbp_far_atr_mult     : Distance at which ``vbp_resistance`` score drops to 0.0.
    """
    rsi_overbought_floor: float = 60.0
    rsi_overbought_range: float = 10.0
    multi_bar_decay_max: float = 3.0
    vol_expansion_ratio: float = 0.5
    vbp_near_atr_mult: float = 0.5
    vbp_far_atr_mult: float = 3.0
    vbp_lookback: int = 120
    vbp_n_bins: int = 24
    vbp_volume_percentile: int = 70


def _load_entry_thresholds(settings: dict) -> EntryThresholds:
    """Load entry thresholds from settings.scanner.entry_thresholds with defaults."""
    raw = settings.get("scanner", {}).get("entry_thresholds", {}) or {}
    defaults = EntryThresholds()
    return EntryThresholds(
        rsi_healthy_center=float(raw.get("rsi_healthy_center", defaults.rsi_healthy_center)),
        rsi_healthy_half_w=float(raw.get("rsi_healthy_half_w", defaults.rsi_healthy_half_w)),
        ma50_slope_scale=float(raw.get("ma50_slope_scale", defaults.ma50_slope_scale)),
        ma200_slope_scale=float(raw.get("ma200_slope_scale", defaults.ma200_slope_scale)),
        breakout_band_pct=float(raw.get("breakout_band_pct", defaults.breakout_band_pct)),
        volume_spike_ratio=float(raw.get("volume_spike_ratio", defaults.volume_spike_ratio)),
        near_52w_high_pct_band=float(
            raw.get("near_52w_high_pct_band", defaults.near_52w_high_pct_band)
        ),
        far_from_52w_low_pct_floor=float(
            raw.get("far_from_52w_low_pct_floor", defaults.far_from_52w_low_pct_floor)
        ),
    )


def _load_exit_thresholds(settings: dict) -> ExitThresholds:
    """Load exit thresholds from settings.scanner.exit_thresholds with defaults."""
    raw = settings.get("scanner", {}).get("exit_thresholds", {}) or {}
    vbp = settings.get("scanner", {}).get("vbp", {}) or {}
    defaults = ExitThresholds()
    return ExitThresholds(
        rsi_overbought_floor=float(raw.get("rsi_overbought_floor", defaults.rsi_overbought_floor)),
        rsi_overbought_range=float(raw.get("rsi_overbought_range", defaults.rsi_overbought_range)),
        multi_bar_decay_max=float(raw.get("multi_bar_decay_max", defaults.multi_bar_decay_max)),
        vol_expansion_ratio=float(raw.get("vol_expansion_ratio", defaults.vol_expansion_ratio)),
        vbp_near_atr_mult=float(raw.get("vbp_near_atr_mult", defaults.vbp_near_atr_mult)),
        vbp_far_atr_mult=float(raw.get("vbp_far_atr_mult", defaults.vbp_far_atr_mult)),
        vbp_lookback=int(vbp.get("lookback", defaults.vbp_lookback)),
        vbp_n_bins=int(vbp.get("n_bins", defaults.vbp_n_bins)),
        vbp_volume_percentile=int(vbp.get("volume_percentile", defaults.vbp_volume_percentile)),
    )


# ── public API ────────────────────────────────────────────────────────────────

class SignalScorer:
    """
    Enrich SignalResult objects with confidence score and description.

    Parameters
    ----------
    settings    : Loaded settings.yaml dict.
    filters_cfg : Loaded filters.yaml dict.
    """

    def __init__(self, settings: dict, filters_cfg: dict) -> None:
        sc = settings.get("scanner", {})
        mh = settings.get("market_hours", {})

        self._entry_weights: dict[str, int] = sc.get("weights", {})
        self._exit_weights: dict[str, int] = sc.get("exit_weights", {})

        # insider_buying / short_interest are backed by placeholder fetchers
        # (Form 4 text-match, yfinance short %). A non-zero weight must not
        # silently shape the score until they are validated — fail loudly.
        for _k in ("insider_buying", "short_interest"):
            if float(self._entry_weights.get(_k, 0) or 0) > 0:
                raise ConfigError(
                    f"scanner.weights.{_k}",
                    reason="must be 0 until the backing fetcher is validated",
                )
        self._min_score: int = sc.get("min_score_to_alert", _DEFAULT_MIN_SCORE)
        self._hold_low: int = mh.get("expected_hold_days_low", _DEFAULT_HOLD_LOW)
        self._hold_high: int = mh.get("expected_hold_days_high", _DEFAULT_HOLD_HIGH)
        self._filters_cfg: dict = filters_cfg
        self._entry_thr: EntryThresholds = _load_entry_thresholds(settings)
        self._exit_thr: ExitThresholds = _load_exit_thresholds(settings)

    def enrich(
            self,
            signal: SignalResult,
            df: pd.DataFrame,
            regime: MarketRegime,
            earnings_date: date | None = None,
            position: Position | None = None,
            market_dfs: dict | None = None,
            vix_df: pd.DataFrame | None = None,
            current_price: float | None = None,
            rp_ranks: dict[str, float] | None = None,
            ticker: str | None = None,
    ) -> None:
        """
        Mutate signal in-place: add ``score``, ``score_components``,
        ``timeframe``, ``expected_hold_days``, ``watch_only``, ``description``.

        Parameters
        ----------
        signal        : SignalResult from FilterEngine.signal(). Mutated.
        df            : Enriched OHLCV DataFrame for this ticker.
        regime        : MarketRegime at signal time.
        earnings_date : Next scheduled earnings date.
        position      : Open Position for this ticker, if any.
        market_dfs    : Symbol → OHLCV for regime indices.
        vix_df        : VIX OHLCV.
        current_price : Latest live price, if available.
        rp_ranks      : Ticker → RP percentile rank [0, 99] (Phase 2).
        """
        if signal.direction in ("long", "short"):
            score, components = _score_entry(
                df, regime, earnings_date, self._entry_weights, self._filters_cfg,
                self._entry_thr,
                market_dfs=market_dfs, signal_type=signal.signal_type,
                rp_ranks=rp_ranks, ticker=ticker,
                direction=signal.direction,
            )
        elif signal.direction in ("exit_long", "exit_short"):
            score, components = _score_exit(
                df, regime, self._exit_weights, self._exit_thr,
                market_dfs=market_dfs,
                direction=signal.direction,
            )
        else:
            score, components = 0.0, {}

        signal.score = round(score, 1)
        signal.score_components = components
        signal.timeframe = _TIMEFRAME
        signal.expected_hold_days = (self._hold_low, self._hold_high)
        signal.watch_only = signal.passed and signal.score < self._min_score

        signal.description = _build_description(
            signal, df, regime, earnings_date, position,
            market_dfs, vix_df, current_price,
        )

        logger.debug(
            "[scorer] %s %s  score=%.1f  alert=%s",
            signal.direction, signal.signal_type,
            signal.score, not signal.watch_only,
        )


# ── direction-flip helpers ────────────────────────────────────────────────────

# Components whose long-biased score has the same shape (0=bad, 1=good) but whose underlying *condition* is
# direction-biased. For shorts, the long-style value is inverted via ``1 - score``. Components NOT in this list are
# either already direction-agnostic (volume_spike, no_earnings_risk) or have their own direction-aware logic (
# rsi_healthy, bb_zscore, short_interest).
_FLIP_FOR_SHORT_ENTRY: tuple[str, ...] = (
    "trend_up",
    "ma50_slope",
    "ma200_slope",
    "breakout_20d",
    "near_52w_high",
    "far_from_52w_low",
    "macd_bullish",
    "relative_strength",
    "insider_buying",
)

# Phase 10 v2: mean-reversion shorts fade an overbought RALLY — they short
# strength *near the highs*. So the "position vs 52w range" axes should keep
# their long-style sense (near the high = good setup), unlike momentum shorts
# which want weakness. We flip everything else but leave those two unflipped.
# (Fuller per-axis mirror functions remain a follow-on; see TODO.)
_FLIP_FOR_SHORT_ENTRY_MR: tuple[str, ...] = tuple(
    k for k in _FLIP_FOR_SHORT_ENTRY
    if k not in ("near_52w_high", "far_from_52w_low")
)

_FLIP_FOR_SHORT_EXIT: tuple[str, ...] = (
    # Exit sub-scores mostly compute "bearish for held longs". For exit_short
    # they need to flip the sense — "bullish surge against held short" is the
    # exit trigger. See ``_score_exit`` for the component list.
    "regime_flip",
    "macd_cross_down",
    "rs_divergence",
)


def _flip_if_short(
        components: dict[str, float],
        direction: str,
        flip_keys: tuple[str, ...] = _FLIP_FOR_SHORT_ENTRY,
) -> None:
    """Mutate ``components`` in place: invert direction-biased scores for shorts.

    No-op when ``direction != "short"``. Only keys present in both
    ``components`` and ``flip_keys`` are touched. Each value is mapped
    ``v -> 1.0 - v`` which preserves the 0-1 range and zeros become ones.

    Notes
    -----
    For Phase 10.4 v1 we accept the asymmetry that this simple inversion
    isn't perfect for mean-reversion shorts (which want different
    geometry than momentum shorts on some axes — e.g. near_52w_high is
    actually GOOD for an MR-short into a rally). Polish in v2.
    """
    if direction != "short":
        return
    for key in flip_keys:
        if key in components:
            components[key] = max(0.0, min(1.0, 1.0 - float(components[key])))


def _ma_series(df: pd.DataFrame, period: int, col: str) -> pd.Series:
    """MA(period) series — the precomputed ``col`` when present (attach_indicators
    uses the same period), else an on-the-fly rolling mean. Reading the column
    avoids an O(n) rolling recompute per scored bar."""
    if col in df.columns:
        return df[col]
    return df["close"].rolling(period, min_periods=period).mean()


# ── entry sub-scores ──────────────────────────────────────────────────────────

def _score_entry(
        df: pd.DataFrame,
        regime: MarketRegime,
        earnings_date: date | None,
        weights: dict[str, int],
        filters_cfg: dict,
        thr: EntryThresholds,
        market_dfs: dict | None = None,
        signal_type: str = "momentum",
        rp_ranks: dict[str, float] | None = None,
        ticker: str | None = None,
        direction: str = "long",
) -> tuple[float, dict[str, float]]:
    """Return (weighted_score_0_to_100, component_dict_0_to_1).

    ``direction`` defaults to ``"long"`` so existing callers stay
    unchanged. Pass ``"short"`` to flip direction-biased components
    (see ``_FLIP_FOR_SHORT_ENTRY``) before the weighted average. The
    flip happens after each component is computed in long-style, so
    the returned ``components`` dict is short-correct."""
    row = df.iloc[-1]
    prev = df.iloc[-2]

    components: dict[str, float] = {}

    # 1. trend_up — close > MA50 > MA200
    ma_fast_s = _ma_series(df, 50, "ma_fast")
    ma_slow_s = _ma_series(df, 200, "ma_slow")
    ma50 = ma_fast_s.iloc[-1]
    ma200 = ma_slow_s.iloc[-1]
    close = float(row["close"])
    if close > ma50 > ma200:
        components["trend_up"] = 1.0
    elif close > ma50:
        components["trend_up"] = 0.5
    else:
        components["trend_up"] = 0.0

    # 2. ma50_slope — MA50 change over last 20 bars as % of price
    ma50_series = ma_fast_s.dropna()
    if len(ma50_series) >= 21:
        slope_pct = (ma50_series.iloc[-1] - ma50_series.iloc[-21]) / ma50_series.iloc[-21] * 100
        components["ma50_slope"] = max(
            0.0,
            min(1.0, (slope_pct + thr.ma50_slope_scale) / (2.0 * thr.ma50_slope_scale)),
        )
    else:
        components["ma50_slope"] = 0.5

    # 2b. ma200_slope — MA200 change over last 20 bars as % of price
    # Mirror of ma50_slope; gated on the weights dict so the sub-score is silently
    # absent (not 0) when the user hasn't enabled it in settings.yaml.
    if "ma200_slope" in weights:
        ma200_series = ma_slow_s.dropna()
        if len(ma200_series) >= 21:
            slope_pct = (
                    (ma200_series.iloc[-1] - ma200_series.iloc[-21])
                    / ma200_series.iloc[-21] * 100
            )
            components["ma200_slope"] = max(
                0.0,
                min(1.0, (slope_pct + thr.ma200_slope_scale) / (2.0 * thr.ma200_slope_scale)),
            )
        else:
            components["ma200_slope"] = 0.5

    # 3. volume_spike — today vs 20-day average (exclude today from average)
    avg_vol = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 22 else 0.0
    if avg_vol > 0:
        vol_ratio = float(row["volume"]) / avg_vol
        components["volume_spike"] = max(0.0, min(1.0, vol_ratio / thr.volume_spike_ratio))
    else:
        components["volume_spike"] = 0.5

    # 4. rsi_healthy — RSI proximity to ideal trend-confirm centre
    rsi_val = float(row["rsi"]) if "rsi" in row.index else 50.0
    components["rsi_healthy"] = max(
        0.0,
        1.0 - abs(rsi_val - thr.rsi_healthy_center) / thr.rsi_healthy_half_w,
    )

    # 5. breakout_20d — close vs prior 20-bar high
    if len(df) >= 21:
        prior_high = float(df["high"].iloc[-21:-1].max())
        gap_pct = (close - prior_high) / prior_high * 100
        if close > prior_high:
            components["breakout_20d"] = 1.0
        elif gap_pct > -thr.breakout_band_pct:
            components["breakout_20d"] = max(0.0, (gap_pct + thr.breakout_band_pct) / thr.breakout_band_pct)
        else:
            components["breakout_20d"] = 0.0
    else:
        components["breakout_20d"] = 0.5

    # 5b. near_52w_high — proximity to trailing 252-bar high (Minervini #9)
    # 1.0 within 5% of the 52w high; linear decay to 0.0 at near_52w_high_pct_band.
    if "near_52w_high" in weights:
        components["near_52w_high"] = _score_near_52w_high(df, close, thr)

    # 5c. far_from_52w_low — leadership floor (Minervini #8)
    # 1.0 when ≥ far_from_52w_low_pct_floor % above the 252-bar low;
    # linear scale from 0.0 (at the low) to 1.0 (at the floor).
    if "far_from_52w_low" in weights:
        components["far_from_52w_low"] = _score_far_from_52w_low(df, close, thr)

    # 6. macd_bullish — histogram sign and direction
    hist = float(row["macd_hist"]) if "macd_hist" in row.index else 0.0
    prev_hist = float(prev["macd_hist"]) if "macd_hist" in prev.index else 0.0
    pos = hist > 0
    growing = hist > prev_hist
    if pos and growing:
        components["macd_bullish"] = 1.0
    elif pos and not growing:
        components["macd_bullish"] = 0.6
    elif not pos and growing:
        components["macd_bullish"] = 0.3
    else:
        components["macd_bullish"] = 0.0

    # 7. no_earnings_risk — days to next report vs configured buffer
    buffer = filters_cfg.get("events", {}).get("earnings_buffer_days", 5)
    if earnings_date is None:
        components["no_earnings_risk"] = 1.0
    else:
        today = df.index[-1].date()  # use bar date, not wall-clock date
        days_to = (earnings_date - today).days
        if days_to <= 0:
            components["no_earnings_risk"] = 1.0
        elif days_to > buffer * 3:
            components["no_earnings_risk"] = 1.0
        else:
            components["no_earnings_risk"] = max(0.0, min(1.0, days_to / (buffer * 3)))

    # 8. relative_strength — ticker outperforming SPY over 20d and 60d
    if "relative_strength" in weights:
        components["relative_strength"] = _score_rs_entry(df, market_dfs)

    # 9. weekly_trend — daily signal agrees with the weekly trend
    if "weekly_trend" in weights:
        components["weekly_trend"] = _score_weekly_trend(df)

    # 10. bb_zscore — Bollinger Band Z-score statistical positioning
    if "bb_zscore" in weights:
        components["bb_zscore"] = _score_bb_zscore(df, signal_type)

    # 11. rp_percentile — cross-sectional relative strength rank (Phase 2)
    if "rp_percentile" in weights:
        components["rp_percentile"] = _score_rp_percentile(ticker, rp_ranks)

    # 12. insider_buying — SEC Form 4 cluster buying (Phase 8)
    if "insider_buying" in weights:
        components["insider_buying"] = _score_insider_buying(ticker)

    # 13. short_interest — yfinance short percent of float (Phase 8)
    if "short_interest" in weights:
        components["short_interest"] = _score_short_interest(
            ticker, regime.trend)

    # Phase 10.4: invert direction-biased components for short entries.
    # Has no effect when direction == "long" (the default).
    # Phase 10 v2: mean-reversion shorts keep the 52w-range axes long-style
    # (they fade strength near the highs), so they use a narrower flip list.
    flip_keys = (
        _FLIP_FOR_SHORT_ENTRY_MR if signal_type == "mean_reversion"
        else _FLIP_FOR_SHORT_ENTRY
    )
    _flip_if_short(components, direction, flip_keys)

    return _weighted_average(components, weights), components


# ── exit sub-scores ───────────────────────────────────────────────────────────

def _score_exit(
        df: pd.DataFrame,
        regime: MarketRegime,
        weights: dict[str, int],
        thr: ExitThresholds,
        market_dfs: dict | None = None,
        direction: str = "exit_long",
) -> tuple[float, dict[str, float]]:
    """Return (weighted_score_0_to_100, component_dict_0_to_1)."""
    row = df.iloc[-1]
    prev_row = df.iloc[-2]
    components: dict[str, float] = {}

    # 1. regime_flip — not BULL is the strongest exit trigger
    components["regime_flip"] = 1.0 if regime.trend != "BULL" else 0.0

    # 2. multi_bar_decay — consecutive negative macd_hist bars
    hist_tail = df["macd_hist"].tail(5).values
    neg_streak = 0
    for h in reversed(hist_tail):
        if h < 0:
            neg_streak += 1
        else:
            break
    components["multi_bar_decay"] = min(1.0, neg_streak / thr.multi_bar_decay_max)

    # 3. rsi_overbought — RSI above the configured floor saturates at floor + range
    rsi_val = float(row["rsi"]) if "rsi" in row.index else 50.0
    components["rsi_overbought"] = max(
        0.0,
        min(1.0, (rsi_val - thr.rsi_overbought_floor) / thr.rsi_overbought_range),
    )

    # 4. macd_cross_down — histogram crossing below zero
    hist = float(row["macd_hist"]) if "macd_hist" in row.index else 0.0
    prev_hist = float(prev_row["macd_hist"]) if "macd_hist" in prev_row.index else 0.0
    if prev_hist > 0 and hist < 0:
        components["macd_cross_down"] = 1.0  # just crossed this bar
    elif hist < 0:
        components["macd_cross_down"] = 0.5  # already below
    else:
        components["macd_cross_down"] = 0.0

    # 5. vol_expansion — ATR today vs 5-bar average
    atr_today = float(row["atr"])
    atr_5d_avg = float(df["atr"].tail(6).iloc[:-1].mean())
    if atr_5d_avg > 0:
        atr_ratio = atr_today / atr_5d_avg
        components["vol_expansion"] = max(
            0.0,
            min(1.0, (atr_ratio - 1.0) / thr.vol_expansion_ratio),
        )
    else:
        components["vol_expansion"] = 0.0

    # 6. rs_divergence — ticker underperforming SPY over 20d
    if "rs_divergence" in weights:
        components["rs_divergence"] = _score_rs_exit(df, market_dfs)

    # 7. vbp_resistance — overhead high-volume node as exit resistance
    if "vbp_resistance" in weights:
        components["vbp_resistance"] = _score_vbp_resistance(df, thr)

    # Phase 10.4: invert direction-biased components for exit_short.
    _flip_if_short(
        components,
        "short" if direction == "exit_short" else "long",
        _FLIP_FOR_SHORT_EXIT,
    )
    return _weighted_average(components, weights), components


# ── sub-score helpers ─────────────────────────────────────────────────────────


def _score_near_52w_high(
        df: pd.DataFrame,
        close: float,
        thr: EntryThresholds,
) -> float:
    """
    Proximity-to-52-week-high score (Minervini criterion #9).

    Computes the trailing 252-bar high from ``df["high"]`` and measures the
    current close's distance below it as a percentage.

        distance_pct = (high_52w − close) / high_52w × 100

    Returns
    -------
    1.0   distance_pct ≤ 5%%               (right at the highs)
    0.0   distance_pct ≥ near_52w_high_pct_band   (typically 25%%)
    linear in between
    0.5   when fewer than 252 bars are available (warmup)

    Notes
    -----
    Uses the trailing high from ``df["high"]`` (intraday high, not close)
    because the user's mental model of "52-week high" follows price
    extremes, not closing prices.
    """
    if len(df) < 252:
        return 0.5
    high_52w = float(df["high"].iloc[-252:].max())
    if high_52w <= 0:
        return 0.5
    distance_pct = (high_52w - close) / high_52w * 100.0
    # Distance ≤ 5% → full score; distance ≥ band → zero score.
    if distance_pct <= 5.0:
        return 1.0
    band = thr.near_52w_high_pct_band
    if distance_pct >= band:
        return 0.0
    # Linear decay in (5, band).
    return max(0.0, 1.0 - (distance_pct - 5.0) / (band - 5.0))


def _score_far_from_52w_low(
        df: pd.DataFrame,
        close: float,
        thr: EntryThresholds,
) -> float:
    """
    Leadership-floor score (Minervini criterion #8).

    Measures how far the current close has advanced from the trailing
    252-bar low, as a percentage of that low.

        above_pct = (close − low_52w) / low_52w × 100

    Returns
    -------
    1.0   above_pct ≥ far_from_52w_low_pct_floor   (typically 30%%)
    0.0   above_pct ≤ 0   (at or below the low)
    linear in between
    0.5   when fewer than 252 bars are available (warmup)
    """
    if len(df) < 252:
        return 0.5
    low_52w = float(df["low"].iloc[-252:].min())
    if low_52w <= 0:
        return 0.5
    above_pct = (close - low_52w) / low_52w * 100.0
    floor = thr.far_from_52w_low_pct_floor
    if above_pct >= floor:
        return 1.0
    if above_pct <= 0:
        return 0.0
    return above_pct / floor


def _score_rs_entry(
        df: pd.DataFrame,
        market_dfs: dict | None,
) -> float:
    """
    Relative strength vs SPY over 20 and 60 trading days.

        RS_n = (ticker_now / ticker_-n) / (SPY_now / SPY_-n) − 1

    Returns
    -------
    1.0   both RS20 and RS60 positive
    0.7   only RS20 positive
    0.4   only RS60 positive
    0.0   both negative
    0.5   insufficient data or SPY unavailable
    """
    if not market_dfs or "SPY" not in market_dfs:
        return 0.5
    spy = market_dfs["SPY"]
    if len(df) < 62 or len(spy) < 62:
        return 0.5

    try:
        t0 = float(df["close"].iloc[-1])
        t20 = float(df["close"].iloc[-21])
        t60 = float(df["close"].iloc[-61])
        s0 = float(spy["close"].iloc[-1])
        s20 = float(spy["close"].iloc[-21])
        s60 = float(spy["close"].iloc[-61])

        rs20 = (t0 / t20) / (s0 / s20) - 1.0 if s20 > 0 and t20 > 0 else 0.0
        rs60 = (t0 / t60) / (s0 / s60) - 1.0 if s60 > 0 and t60 > 0 else 0.0

        if rs20 > 0 and rs60 > 0:
            return 1.0
        elif rs20 > 0:
            return 0.7
        elif rs60 > 0:
            return 0.4
        else:
            return 0.0
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
        logger.debug("rs_entry score failed (neutral fallback): %s", exc)
        return 0.5


def _score_rs_exit(
        df: pd.DataFrame,
        market_dfs: dict | None,
) -> float:
    """
    Exit signal from relative-strength divergence vs SPY over 20 days.

        score = clamp01(-RS20 * 10)

    Returns 0.5 when SPY data is unavailable or insufficient.
    """
    if not market_dfs or "SPY" not in market_dfs:
        return 0.5
    spy = market_dfs["SPY"]
    if len(df) < 22 or len(spy) < 22:
        return 0.5

    try:
        t0 = float(df["close"].iloc[-1])
        t20 = float(df["close"].iloc[-21])
        s0 = float(spy["close"].iloc[-1])
        s20 = float(spy["close"].iloc[-21])
        if s20 <= 0 or t20 <= 0:
            return 0.5
        rs20 = (t0 / t20) / (s0 / s20) - 1.0
        # rs20 < 0 means underperforming → exit signal
        return max(0.0, min(1.0, -rs20 * _RS_EXIT_SCALE))
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
        logger.debug("rs_exit score failed (neutral fallback): %s", exc)
        return 0.5


def _score_weekly_trend(df: pd.DataFrame) -> float:
    """
    Higher-timeframe agreement: does the weekly trend support the daily signal?

    Resamples daily close to weekly (W-FRI), computes a 10-week SMA, then:
        A. weekly close > 10-week SMA
        B. 10-week SMA rising vs 4 weeks ago

    Returns
    -------
    1.0   both A and B
    0.6   A only
    0.0   A fails
    0.5   < 14 weekly bars
    """
    if len(df) < 70:  # need ~14 weeks at minimum
        return 0.5

    try:
        weekly = df["close"].resample("W-FRI").last().dropna()
        if len(weekly) < 14:
            return 0.5

        sma10 = weekly.rolling(10, min_periods=10).mean()
        last_sma = sma10.iloc[-1]
        if pd.isna(last_sma):
            return 0.5

        last_close = float(weekly.iloc[-1])
        above_sma = last_close > float(last_sma)

        # SMA rising: compare to SMA value 4 weeks ago
        sma_valid = sma10.dropna()
        if len(sma_valid) >= 5:
            sma_rising = float(sma_valid.iloc[-1]) > float(sma_valid.iloc[-5])
        else:
            sma_rising = False

        if above_sma and sma_rising:
            return 1.0
        elif above_sma:
            return 0.6
        else:
            return 0.0
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, AttributeError) as exc:
        logger.debug("weekly_trend score failed (neutral fallback): %s", exc)
        return 0.5


def _score_bb_zscore(df: pd.DataFrame, signal_type: str) -> float:
    """
    Bollinger Band Z-score scoring, signal-type-aware.

        Z = (close − SMA₂₀) / σ₂₀

    Momentum entry (trend-following):
        score = max(0, 1 − |Z| / 2)             — best near Z=0
    Mean-reversion entry (statistical dip):
        score = clamp01((−Z − 0.5) / 2)         — best at Z ≪ 0

    Returns 0.5 when ``bb_z`` is NaN or absent.
    """
    bb_z = None
    if "bb_z" in df.columns:
        raw = df["bb_z"].iloc[-1]
        if not pd.isna(raw):
            bb_z = float(raw)

    if bb_z is None:
        return 0.5

    if signal_type == "mean_reversion":
        # Deeply oversold is desirable
        return max(0.0, min(1.0, (-bb_z - 0.5) / _BB_Z_HALFWIDTH))
    else:
        # Momentum: near mean is ideal, extremes in either direction are bad
        return max(0.0, 1.0 - abs(bb_z) / _BB_Z_HALFWIDTH)


def _score_vbp_resistance(
        df: pd.DataFrame,
        thr: ExitThresholds,
) -> float:
    """
    Exit sub-score from Volume-by-Price overhead resistance.

    Computes a VBP histogram over the trailing 120 bars and finds the
    nearest high-volume node *above* the current price.  The score is
    based on the distance to that node in ATR units:

        1.0  when within 0.5 × ATR  (price approaching resistance)
        0.0  when ≥ 3.0 × ATR away  (plenty of room to run)
        linear decay in between

    Returns 0.0 when no qualifying node is found.
    """
    from core.indicators.vbp import compute_vbp, nearest_high_volume_node_above

    if len(df) < 120:
        return 0.0

    vbp = compute_vbp(df, lookback=thr.vbp_lookback, n_bins=thr.vbp_n_bins)
    if vbp.empty:
        return 0.0

    close = float(df.iloc[-1]["close"])
    atr = float(df.iloc[-1]["atr"])
    if atr <= 0:
        return 0.0

    node = nearest_high_volume_node_above(vbp, close, volume_percentile=thr.vbp_volume_percentile)
    if node is None:
        return 0.0

    node_price = node[0]
    distance_atr = (node_price - close) / atr

    near = thr.vbp_near_atr_mult
    far = thr.vbp_far_atr_mult
    if distance_atr <= near:
        return 1.0
    if distance_atr >= far:
        return 0.0
    return max(0.0, 1.0 - (distance_atr - near) / (far - near))


def _score_rp_percentile(
        ticker: str | None,
        rp_ranks: dict[str, float] | None,
) -> float:
    """
    Cross-sectional percentile-rank relative strength sub-score (Phase 2).

    Sub-score mapping:
        rank >= 80 → 1.0
        rank 70–80 → 0.7
        rank 50–70 → 0.3
        rank < 50  → 0.0
        missing    → 0.5
    """
    if ticker is None or rp_ranks is None:
        return 0.5
    rank = rp_ranks.get(ticker)
    if rank is None:
        return 0.5
    if rank >= 80:
        return 1.0
    elif rank >= 70:
        return 0.7
    elif rank >= 50:
        return 0.3
    else:
        return 0.0


def _score_insider_buying(ticker: str | None) -> float:
    """
    SEC Form 4 insider buying sub-score (Phase 8).

    Scoring:
        1.0 — cluster buy in last 30d (≥3 distinct insiders, ≥$250k)
        0.7 — 2 distinct insider buys ≥$100k in 90d
        0.5 — 1 buy or no signal
        0.0 — net selling >$1M in 90d
    """
    if ticker is None:
        return 0.5
    try:
        from core.fetchers.behavioral.form4 import fetch_form4
        data = fetch_form4(ticker)
    except (ImportError, KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        logger.debug("form4 fetch for %s failed (neutral fallback): %s", ticker, exc)
        return 0.5

    buys_30 = data.get("buys_30d", 0)
    buys_90 = data.get("buys_90d", 0)
    sells_90 = data.get("sells_90d", 0)
    buy_val_30 = data.get("buy_value_30d", 0.0)
    sell_val_90 = data.get("sell_value_90d", 0.0)
    cluster = data.get("cluster_buy_30d", False)

    if cluster and buy_val_30 >= 250_000:
        return 1.0
    if buys_90 >= 2 and buy_val_30 >= 100_000:
        return 0.7
    if sell_val_90 > 1_000_000 and sells_90 > buys_90:
        return 0.0
    if buys_30 > 0 or buys_90 > 0:
        return 0.5
    return 0.5


def _score_short_interest(
        ticker: str | None,
        trend: str = "BULL",
) -> float:
    """
    Short interest sub-score (Phase 8).

    BULL regime:
        SI < 3%     → 0.7 (low short interest, healthy)
        SI > 20%    → 0.8 (squeeze candidate)
        normal      → 0.5

    BEAR regime:
        SI > 10%    → 0.0 (high short interest, risky)
        SI < 3%     → 0.6
        normal      → 0.3
    """
    if ticker is None:
        return 0.5
    try:
        from core.fetchers.behavioral.short_interest import fetch_short_interest
        data = fetch_short_interest(ticker)
    except (ImportError, KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        logger.debug("short_interest fetch for %s failed (neutral fallback): %s", ticker, exc)
        return 0.5

    si_pct = data.get("short_percent_of_float")
    if si_pct is None:
        return 0.5

    si_pct = float(si_pct) * 100  # convert to percentage

    if trend == "BULL":
        if si_pct < 3:
            return 0.7
        elif si_pct > 20:
            return 0.8
        else:
            return 0.5
    else:
        if si_pct > 10:
            return 0.0
        elif si_pct < 3:
            return 0.6
        else:
            return 0.3


# ── description builder ───────────────────────────────────────────────────────

def _build_description(
        signal: SignalResult,
        df: pd.DataFrame,
        regime: MarketRegime,
        earnings_date: date | None,
        position: Position | None,
        market_dfs: dict | None,
        vix_df: pd.DataFrame | None,
        current_price: float | None = None,
) -> str:
    """
    Build a multi-line human-readable description attached to every signal.

    Lines:
        Line 0: score + hold horizon (entries) | score (exits)
        Line 1: signal-bar snapshot (close, RSI, MACD hist+Δ, ATR%, vol×)
        Line 2: current live price drift from signal bar (when available)
        Line 3: regime context (index distances from MA50, VIX)
        Line 4: earnings proximity (entries only)
        Line 5: score component breakdown
        Line 6: position P&L (exits only)
    """
    row = df.iloc[-1]
    prev_row = df.iloc[-2]
    close = float(row["close"])
    rsi_val = float(row["rsi"]) if "rsi" in row.index else float("nan")
    hist = float(row["macd_hist"]) if "macd_hist" in row.index else float("nan")
    prev_hist = float(prev_row["macd_hist"]) if "macd_hist" in prev_row.index else float("nan")
    hist_delta = hist - prev_hist
    atr_val = float(row["atr"])
    atr_pct = atr_val / close * 100 if close > 0 else 0.0
    vol_ratio = _vol_ratio(df)

    lines: list[str] = []

    # Line 0: summary
    if signal.direction == "long":
        lines.append(
            f"score {signal.score:.0f}/100"
            f"  hold ~{signal.expected_hold_days[0]}–{signal.expected_hold_days[1]}d"
        )
    elif signal.direction == "exit_long":
        lines.append(f"score {signal.score:.0f}/100")

    # Line 1: signal-bar snapshot
    sign = "+" if hist_delta >= 0 else ""
    bar_date = df.index[-1].strftime("%Y-%m-%d")
    lines.append(
        f"signal bar {bar_date}  close={close:.2f}"
        f"  RSI={rsi_val:.1f}"
        f"  MACD hist={hist:+.3f}(Δ{sign}{hist_delta:.3f})"
        f"  ATR={atr_val:.2f}({atr_pct:.1f}%)"
        f"  vol×{vol_ratio:.1f}"
    )

    # Line 2: current price drift (only when live price is available)
    if current_price is not None and close > 0:
        drift_pct = (current_price - close) / close * 100
        drift_sign = "+" if drift_pct >= 0 else ""
        lines.append(
            f"current price={current_price:.2f}"
            f"  drift {drift_sign}{drift_pct:.2f}% vs signal bar"
        )

    # Line 3: regime context
    regime_detail = _regime_detail(regime, market_dfs, vix_df)
    lines.append(f"regime: {regime_detail}")

    # Line 4: earnings (entries only)
    if signal.direction == "long" and earnings_date is not None:
        bar_date_d = df.index[-1].date()
        days_to = (earnings_date - bar_date_d).days
        if days_to > 0:
            lines.append(f"earnings: {earnings_date.isoformat()} ({days_to}d away)")

    # Line 5: component breakdown
    if signal.score_components:
        parts = "  ".join(
            f"{k}={v:.2f}" for k, v in signal.score_components.items()
        )
        lines.append(f"components: {parts}")

    # Line 6: position P&L for exits
    if signal.direction == "exit_long" and position is not None:
        pnl_pct = (close - position.entry_price) / position.entry_price * 100
        hold_days = (df.index[-1].date() - position.entry_date).days
        pnl_sign = "+" if pnl_pct >= 0 else ""
        lines.append(
            f"position: opened {position.entry_date.isoformat()}"
            f" @ {position.entry_price:.2f}"
            f"  held {hold_days}d"
            f"  unrealized {pnl_sign}{pnl_pct:.1f}%"
        )

    return "\n  ".join(lines)


# ── helpers ───────────────────────────────────────────────────────────────────

def _weighted_average(
        components: dict[str, float],
        weights: dict[str, int],
) -> float:
    """
    Weighted average of component scores scaled to [0, 100].

    Components absent from ``weights`` are ignored. Returns 0.0 when the
    total weight is zero.
    """
    total_weight = sum(weights.get(k, 0) for k in components)
    if total_weight == 0:
        return 0.0
    weighted_sum = sum(
        components[k] * weights.get(k, 0) for k in components
    )
    return weighted_sum / total_weight * 100.0


def _vol_ratio(df: pd.DataFrame) -> float:
    """Today's volume vs prior 20-bar average."""
    if len(df) < 22:
        return 1.0
    avg = float(df["volume"].iloc[-21:-1].mean())
    return float(df["volume"].iloc[-1]) / avg if avg > 0 else 0.0


def _regime_detail(
        regime: MarketRegime,
        market_dfs: dict | None,
        vix_df: pd.DataFrame | None,
) -> str:
    """Human-readable regime summary with index distances and VIX."""
    parts: list[str] = [regime.label]

    if market_dfs:
        for sym, idx_df in market_dfs.items():
            if idx_df is None or len(idx_df) < 50:
                continue
            last = float(idx_df["close"].iloc[-1])
            ma50 = float(idx_df["close"].iloc[-50:].mean())
            if ma50 > 0:
                dist = (last - ma50) / ma50 * 100
                sign = "+" if dist >= 0 else ""
                parts.append(f"{sym} {sign}{dist:.1f}% vs MA50")

    if vix_df is not None and not vix_df.empty:
        vix = float(vix_df["close"].iloc[-1])
        parts.append(f"VIX={vix:.1f}")

    return " | ".join(parts)
