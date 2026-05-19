"""
Confidence scoring for entry and exit signals.

Enriches a SignalResult in-place after FilterEngine fires. Weights and
threshold dataclasses are loaded from settings.yaml.

Entry sub-scores  (each in [0, 1], weighted-averaged to [0, 100])
    trend_up           close > MA50 > MA200 stack alignment
    ma50_slope         MA50 rising or falling over last 20 bars
    volume_spike       today's volume vs 20-day average
    rsi_healthy        RSI proximity to the ideal trend-confirm band
    breakout_20d       close vs prior 20-bar high
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

Signals where the trigger fired but ``score < min_score_to_alert`` are
marked ``watch_only=True``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from core.filter_engine import MarketRegime, SignalResult
    from core.position_manager import Position

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_TIMEFRAME = "daily"
_DEFAULT_HOLD_LOW = 10
_DEFAULT_HOLD_HIGH = 15
_DEFAULT_MIN_SCORE = 60


# ── entry-side scoring thresholds (settings.yaml → scanner.entry_thresholds) ──

@dataclass(frozen=True)
class EntryThresholds:
    """Tunable constants used by entry sub-scores.

    Attributes
    ----------
    rsi_healthy_center : RSI value mapped to score 1.0 for trend-confirm band.
    rsi_healthy_half_w : Half-width of the band; score 0.0 at center ± width.
    ma50_slope_scale   : 20-bar MA50 % slope scaled by (slope + scale) / (2*scale).
    breakout_band_pct  : Below prior 20d high by this many %% maps linearly to 0.
    volume_spike_ratio : Today's volume / 20d avg at which score saturates to 1.0.
    """
    rsi_healthy_center: float = 52.5
    rsi_healthy_half_w: float = 12.5
    ma50_slope_scale: float = 2.0
    breakout_band_pct: float = 3.0
    volume_spike_ratio: float = 2.0


@dataclass(frozen=True)
class ExitThresholds:
    """Tunable constants used by exit sub-scores.

    Attributes
    ----------
    rsi_overbought_floor : RSI value at which rsi_overbought score becomes > 0.
    rsi_overbought_range : RSI delta over the floor at which score saturates to 1.0.
    multi_bar_decay_max  : Consecutive negative macd_hist bars saturating score at 1.0.
    vol_expansion_ratio  : ATR(today)/ATR(5d avg) − 1 saturating score at 1.0.
    """
    rsi_overbought_floor: float = 60.0
    rsi_overbought_range: float = 10.0
    multi_bar_decay_max: float = 3.0
    vol_expansion_ratio: float = 0.5


def _load_entry_thresholds(settings: dict) -> EntryThresholds:
    """Load entry thresholds from settings.scanner.entry_thresholds with defaults."""
    raw = settings.get("scanner", {}).get("entry_thresholds", {}) or {}
    defaults = EntryThresholds()
    return EntryThresholds(
        rsi_healthy_center=float(raw.get("rsi_healthy_center", defaults.rsi_healthy_center)),
        rsi_healthy_half_w=float(raw.get("rsi_healthy_half_w", defaults.rsi_healthy_half_w)),
        ma50_slope_scale=float(raw.get("ma50_slope_scale", defaults.ma50_slope_scale)),
        breakout_band_pct=float(raw.get("breakout_band_pct", defaults.breakout_band_pct)),
        volume_spike_ratio=float(raw.get("volume_spike_ratio", defaults.volume_spike_ratio)),
    )


def _load_exit_thresholds(settings: dict) -> ExitThresholds:
    """Load exit thresholds from settings.scanner.exit_thresholds with defaults."""
    raw = settings.get("scanner", {}).get("exit_thresholds", {}) or {}
    defaults = ExitThresholds()
    return ExitThresholds(
        rsi_overbought_floor=float(raw.get("rsi_overbought_floor", defaults.rsi_overbought_floor)),
        rsi_overbought_range=float(raw.get("rsi_overbought_range", defaults.rsi_overbought_range)),
        multi_bar_decay_max=float(raw.get("multi_bar_decay_max", defaults.multi_bar_decay_max)),
        vol_expansion_ratio=float(raw.get("vol_expansion_ratio", defaults.vol_expansion_ratio)),
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
        """
        if signal.direction == "long":
            score, components = _score_entry(
                df, regime, earnings_date, self._entry_weights, self._filters_cfg,
                self._entry_thr,
                market_dfs=market_dfs, signal_type=signal.signal_type,
            )
        elif signal.direction == "exit_long":
            score, components = _score_exit(
                df, regime, self._exit_weights, self._exit_thr,
                market_dfs=market_dfs,
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
) -> tuple[float, dict[str, float]]:
    """Return (weighted_score_0_to_100, component_dict_0_to_1)."""
    row = df.iloc[-1]
    prev = df.iloc[-2]

    components: dict[str, float] = {}

    # 1. trend_up — close > MA50 > MA200
    ma50 = df["close"].rolling(50, min_periods=50).mean().iloc[-1]
    ma200 = df["close"].rolling(200, min_periods=200).mean().iloc[-1]
    close = float(row["close"])
    if close > ma50 > ma200:
        components["trend_up"] = 1.0
    elif close > ma50:
        components["trend_up"] = 0.5
    else:
        components["trend_up"] = 0.0

    # 2. ma50_slope — MA50 change over last 20 bars as % of price
    ma50_series = df["close"].rolling(50, min_periods=50).mean().dropna()
    if len(ma50_series) >= 21:
        slope_pct = (ma50_series.iloc[-1] - ma50_series.iloc[-21]) / ma50_series.iloc[-21] * 100
        components["ma50_slope"] = max(
            0.0,
            min(1.0, (slope_pct + thr.ma50_slope_scale) / (2.0 * thr.ma50_slope_scale)),
        )
    else:
        components["ma50_slope"] = 0.5

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

    return _weighted_average(components, weights), components


# ── exit sub-scores ───────────────────────────────────────────────────────────

def _score_exit(
        df: pd.DataFrame,
        regime: MarketRegime,
        weights: dict[str, int],
        thr: ExitThresholds,
        market_dfs: dict | None = None,
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

    return _weighted_average(components, weights), components


# ── sub-score helpers ─────────────────────────────────────────────────────────

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
    except Exception:
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
        return max(0.0, min(1.0, -rs20 * 10.0))
    except Exception:
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
    except Exception:
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
        return max(0.0, min(1.0, (-bb_z - 0.5) / 2.0))
    else:
        # Momentum: near mean is ideal, extremes in either direction are bad
        return max(0.0, 1.0 - abs(bb_z) / 2.0)


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
            ma50 = float(idx_df["close"].rolling(50, min_periods=50).mean().iloc[-1])
            if ma50 > 0:
                dist = (last - ma50) / ma50 * 100
                sign = "+" if dist >= 0 else ""
                parts.append(f"{sym} {sign}{dist:.1f}% vs MA50")

    if vix_df is not None and not vix_df.empty:
        vix = float(vix_df["close"].iloc[-1])
        parts.append(f"VIX={vix:.1f}")

    return " | ".join(parts)
