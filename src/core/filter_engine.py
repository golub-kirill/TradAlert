"""
Two-stage filter pipeline.

    scan()    structural quality gate, always runs
    signal()  long-entry detection with regime, trend, and earnings gating

Indicators (RSI, MACD, ATR) must be present on the input DataFrame.
Moving averages used for trend classification are computed internally.

The engine is stateless. Construct once, call per ticker per bar. All
market data is supplied by the caller; no I/O happens after construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml

from exceptions import InsufficientDataError

logger = logging.getLogger(__name__)


# ── type aliases ──────────────────────────────────────────────────────────────

TrendState  = Literal["BULL", "BEAR", "CHOP"]
VolState    = Literal["LOW", "NORMAL", "HIGH"]
TickerTrend = Literal["UPTREND", "DOWNTREND", "CHOP"]
Direction   = Literal["long", "exit_long", "none"]
SignalType  = Literal["momentum", "mean_reversion", "regime_exit", "none"]


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class MarketRegime:
    """
    Two-axis classification of the broad market.

    Attributes
    ----------
    trend      : "BULL" | "BEAR" | "CHOP"
        Computed from regime.index_symbols (default SPY + QQQ) vs each
        index's own MA50. BULL when indices agree they are above MA50;
        BEAR when they agree below; CHOP when they disagree.
    volatility : "LOW" | "NORMAL" | "HIGH"
        Computed from VIX close vs regime.vix_low / regime.vix_high
        thresholds (defaults 20 / 25). Defaults to NORMAL when vix_df
        is not supplied — no high-vol cutoff is then applied.
    """
    trend:      TrendState
    volatility: VolState

    @property
    def label(self) -> str:
        """Combined label such as "BULL_LOW" — used in logging and downstream filters."""
        return f"{self.trend}_{self.volatility}"

    @property
    def allows_longs(self) -> bool:
        """Long signals require BULL trend and not-HIGH volatility."""
        return self.trend == "BULL" and self.volatility != "HIGH"

    @property
    def allows_shorts(self) -> bool:
        """Short signals require BEAR trend and not-HIGH volatility."""
        return self.trend == "BEAR" and self.volatility != "HIGH"


@dataclass
class ScanResult:
    """
    Output of FilterEngine.scan().

    Attributes
    ----------
    passed     : True when all scan filters cleared.
    reason     : Human-readable explanation. Always populated.
    close      : Last bar close price. None when scan raised before compute.
    atr        : ATR(14) on the last bar.
    atr_pct    : atr / close × 100.
    dv20       : 20-day average dollar volume.
    market_cap : Market cap in dollars; None when not supplied (ETF/index).
    rsi        : RSI(14) on the last bar.
    macd       : MACD line on the last bar.
    macd_signal: MACD signal line on the last bar.
    macd_hist  : MACD histogram on the last bar.
    """
    passed:      bool
    reason:      str        = ""

    # ── last-bar snapshot (populated inside scan()) ──────────────────────────
    close:       float | None = field(default=None, repr=False)
    atr:         float | None = field(default=None, repr=False)
    atr_pct:     float | None = field(default=None, repr=False)
    dv20:        float | None = field(default=None, repr=False)
    market_cap:  float | None = field(default=None, repr=False)
    rsi:         float | None = field(default=None, repr=False)
    macd:        float | None = field(default=None, repr=False)
    macd_signal: float | None = field(default=None, repr=False)
    macd_hist:   float | None = field(default=None, repr=False)


@dataclass
class SignalResult:
    """
    Output of FilterEngine.signal().

    Attributes
    ----------
    passed            : True when a signal fired and all gates cleared.
    direction         : "long" | "exit_long" | "none".
    signal_type       : "momentum" | "mean_reversion" | "regime_exit" | "none".
    stop_price        : close ± ATR × atr_multiplier.
    target_price      : close ± risk × min_rr, where risk = abs(close - stop_price).
    min_rr            : Minimum risk:reward ratio from config.
    size_mult         : Position-size multiplier. Always 1.0; reserved.
    market_regime     : Regime label at signal time, e.g. "BULL_NORMAL".
    ticker_trend      : "UPTREND" | "DOWNTREND" | "CHOP" | "N/A".
    reason            : Human-readable explanation. Always populated.
    score             : Confidence score [0, 100]. 0 until SignalScorer.enrich().
    score_components  : Sub-score dict {name: 0-1}. Empty until enriched.
    timeframe         : "daily".
    expected_hold_days: (low, high) trading day range.
    watch_only        : True when trigger fired but score < min_score_to_alert.
    description       : Multi-line detail block built by SignalScorer.
    """
    passed:             bool
    direction:          Direction  = "none"
    signal_type:        SignalType = "none"
    stop_price:         float      = 0.0
    target_price:       float      = 0.0
    min_rr:             float      = 0.0
    size_mult:          float      = 1.0
    market_regime:      str        = ""
    ticker_trend:       str        = ""
    reason:             str        = ""
    # ── enriched by SignalScorer ──────────────────────────────────────────────
    score:              float               = field(default=0.0,  repr=False)
    score_components:   dict                = field(default_factory=dict, repr=False)
    timeframe:          str                 = field(default="daily",  repr=False)
    expected_hold_days: tuple               = field(default=(10, 15), repr=False)
    watch_only:         bool                = field(default=False,    repr=False)
    description:        str                 = field(default="",       repr=False)


# ── engine ────────────────────────────────────────────────────────────────────

class FilterEngine:
    """
    Stateless two-stage filter engine. Construct once, call per ticker per bar.

    Parameters
    ----------
    config_path : Path to filters.yaml. Default "config/filters.yaml".
    today       : Override "today" for backtesting. Default date.today().
    """

    def __init__(
        self,
        config_path: Path | str = "config/filters.yaml",
        today:       date | None = None,
    ):
        with open(config_path) as f:
            self._cfg = yaml.safe_load(f)
        self._today = today or date.today()

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def scan(
        self,
        ticker:     str,
        df:         pd.DataFrame,
        market_cap: float | None = None,
    ) -> ScanResult:
        """
        Structural quality check. Always runs — never blocked by stop_dates.

        Checks applied in order:
            1. Row count ≥ 20
            2. close ≥ price.min_price
            3. 20-day avg dollar volume ≥ liquidity.min_dollar_volume_20d
            4. market_cap ≥ market_cap.min_market_cap (skipped when None)
            5. ATR% within volatility.min_atr_pct … max_atr_pct

        ATR% = atr / close × 100.

        Last-bar metric snapshot is attached to every returned ScanResult,
        including failing ones, so the DB layer can persist them.

        Parameters
        ----------
        ticker     : Symbol; used for logging only.
        df         : DataFrame with indicators already computed.
                     Required columns: open, high, low, close, volume, atr,
                                       rsi, macd, macd_signal, macd_hist.
        market_cap : Market cap in dollars. None skips the gate (ETFs/indices).

        Returns
        -------
        ScanResult

        Raises
        ------
        InsufficientDataError
            When df has fewer than 20 rows.
        """
        if len(df) < 20:
            raise InsufficientDataError(got=len(df), need=20, ticker=ticker)

        row  = df.iloc[-1]
        dv20 = float((df["close"] * df["volume"]).tail(20).mean())

        # ── capture snapshot now so all exit paths can carry it ───────────────
        def _snapshot(reason: str, passed: bool) -> ScanResult:
            return ScanResult(
                passed      = passed,
                reason      = reason,
                close       = float(row["close"]),
                atr         = float(row["atr"]),
                atr_pct     = float(row["atr"] / row["close"] * 100),
                dv20        = dv20,
                market_cap  = market_cap,
                rsi         = float(row["rsi"])         if "rsi"         in row.index else None,
                macd        = float(row["macd"])        if "macd"        in row.index else None,
                macd_signal = float(row["macd_signal"]) if "macd_signal" in row.index else None,
                macd_hist   = float(row["macd_hist"])   if "macd_hist"   in row.index else None,
            )

        # 1. price floor
        min_price = self._cfg["price"]["min_price"]
        if row["close"] < min_price:
            return _snapshot(f"price {row['close']:.2f} < min {min_price}", False)

        # 2. 20-day average dollar volume
        min_dv = self._cfg["liquidity"]["min_dollar_volume_20d"]
        if dv20 < min_dv:
            return _snapshot(f"avg dollar vol {dv20:,.0f} < min {min_dv:,.0f}", False)

        # 3. market cap floor (skipped for ETFs/indices)
        if market_cap is not None:
            min_mc = self._cfg["market_cap"]["min_market_cap"]
            if market_cap < min_mc:
                return _snapshot(f"market cap {market_cap:,.0f} < min {min_mc:,.0f}", False)

        # 4. ATR as a percentage of price
        atr_pct = row["atr"] / row["close"] * 100
        min_atr = self._cfg["volatility"]["min_atr_pct"]
        max_atr = self._cfg["volatility"]["max_atr_pct"]
        if atr_pct < min_atr:
            return _snapshot(f"ATR% {atr_pct:.2f} < min {min_atr}", False)
        if atr_pct > max_atr:
            return _snapshot(f"ATR% {atr_pct:.2f} > max {max_atr}", False)

        return _snapshot(self._scan_pass_reason(df, row, dv20), True)

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def signal(
        self,
        ticker:        str,
        df:            pd.DataFrame,
        market_dfs:    dict[str, pd.DataFrame] | None = None,
        vix_df:        pd.DataFrame | None = None,
        earnings_date: date | None = None,
        held_long:     bool = False,
    ) -> SignalResult:
        """
        Signal detection. Branches on held_long.

        held_long=False — entry mode. Long-entry detection with regime,
        trend, and earnings gating. Gate order:
            1. stop_date blackout
            2. row-count guard (≥ trend.ma_slow rows)
            3. earnings buffer
            4. entry condition (regime + trend + trigger)
            5. R:R sanity

        held_long=True — exit mode. Exit-signal detection on a currently-held
        long. Gates 1 and 3 are SKIPPED — stop-dates and earnings buffers
        protect new risk, not existing risk. The row-count guard still
        applies. Exit fires on:
            • momentum fade  (macd_hist crosses below zero + RSI confirms)
            • mean-rev exit  (RSI overbought + macd_hist turning down)
            • regime flip    (regime no longer BULL — capital protection)
        Exit signals bypass the regime gate and fire freely under HIGH
        volatility — gap risk is precisely when exits matter most.

        Parameters
        ----------
        ticker        : Symbol; used for logging only.
        df            : DataFrame with indicators present.
                        Required columns: close, atr, rsi, macd_hist.
        market_dfs    : Symbol → OHLCV mapping for regime indices.
        vix_df        : VIX OHLCV. None → volatility defaults to NORMAL.
        earnings_date : Next scheduled earnings date.
        held_long     : True → run exit-signal logic.

        Returns
        -------
        SignalResult

        Raises
        ------
        InsufficientDataError
            When df has fewer than trend.ma_slow rows.
        """
        # Regime is computed in both modes so it can be reported.
        regime = self._market_regime(market_dfs, vix_df)

        if held_long:
            return self._signal_exit(ticker, df, regime)
        return self._signal_entry(ticker, df, regime, earnings_date)

    # ── Stage 2: entry mode ──────────────────────────────────────────────────

    def _signal_entry(
        self,
        ticker:        str,
        df:            pd.DataFrame,
        regime:        MarketRegime,
        earnings_date: date | None,
    ) -> SignalResult:
        """Long-entry signal detection with full gate chain."""
        # 1. stop_date blackout
        blocked, reason = self._signal_blocked()
        if blocked:
            return SignalResult(
                False, reason=reason,
                market_regime=regime.label, ticker_trend="N/A",
            )

        # 2. row-count guard
        min_rows = max(2, self._cfg["trend"]["ma_slow"])
        if len(df) < min_rows:
            raise InsufficientDataError(got=len(df), need=min_rows, ticker=ticker)

        ticker_trend = self._ticker_trend(df)

        # 3. earnings buffer
        if self._near_earnings(earnings_date):
            buf     = self._cfg["events"]["earnings_buffer_days"]
            days_to = (earnings_date - self._today).days
            return SignalResult(
                False,
                reason=f"earnings in {days_to}d (buffer {buf}d)",
                market_regime=regime.label, ticker_trend=ticker_trend,
            )

        row  = df.iloc[-1]
        prev = df.iloc[-2]

        # 4. evaluate long-entry conditions
        direction, signal_type, why = self._evaluate_entry(
            row, prev, regime, ticker_trend,
        )

        if direction == "none":
            return SignalResult(
                False, reason=why,
                market_regime=regime.label, ticker_trend=ticker_trend,
            )

        # 5. R:R sanity (longs only — direction is always "long" here)
        atr_mult     = self._cfg["signals"]["stop_loss"]["atr_multiplier"]
        min_rr       = self._cfg["signals"]["stop_loss"]["min_rr"]
        stop_dist    = row["atr"] * atr_mult
        stop_price   = row["close"] - stop_dist
        risk         = abs(row["close"] - stop_price)
        target_price = row["close"] + risk * min_rr

        if not self._rr_ok(row["close"], stop_price, min_rr, is_long=True):
            return SignalResult(
                False,
                reason=f"R:R below minimum {min_rr}",
                market_regime=regime.label, ticker_trend=ticker_trend,
            )

        return SignalResult(
            passed        = True,
            direction     = "long",
            signal_type   = signal_type,
            stop_price    = round(stop_price,   4),
            target_price  = round(target_price, 4),
            min_rr        = min_rr,
            size_mult     = 1.0,
            market_regime = regime.label,
            ticker_trend  = ticker_trend,
            reason        = "entry signal fired",
        )

    # ── Stage 2: exit mode ───────────────────────────────────────────────────

    def _signal_exit(
        self,
        ticker: str,
        df:     pd.DataFrame,
        regime: MarketRegime,
    ) -> SignalResult:
        """
        Exit-signal detection for held longs.

        Bypasses stop_date blackout and earnings buffer (those protect new
        risk, not existing risk). Bypasses regime allows_longs gate (an exit
        is the response to a bad regime, not blocked by it). Fires freely
        under HIGH volatility.

        Fires on the first matching condition:
            1. regime flip      — regime no longer BULL
            2. momentum fade    — macd_hist crosses below zero + RSI confirms
            3. mean-rev exit    — RSI overbought + macd_hist turning down

        Each condition is individually gated by signals.exits.<name> in
        filters.yaml (default True when missing — preserves prior behavior).
        Use the toggles for ablation: disable an exit, re-run the backtest,
        compare the new run row in MySQL against the prior.
        """
        # row-count guard still applies — trend label needs MA200
        min_rows = max(2, self._cfg["trend"]["ma_slow"])
        if len(df) < min_rows:
            raise InsufficientDataError(got=len(df), need=min_rows, ticker=ticker)

        ticker_trend = self._ticker_trend(df)
        row          = df.iloc[-1]
        prev         = df.iloc[-2]

        # Per-exit toggles. Missing section → all True (back-compat with old configs).
        exit_cfg = self._cfg.get("signals", {}).get("exits", {})

        # 1. regime flip — any non-BULL regime triggers exit on a held long
        if exit_cfg.get("regime_flip", True) and regime.trend != "BULL":
            return SignalResult(
                passed        = True,
                direction     = "exit_long",
                signal_type   = "regime_exit",
                stop_price    = 0.0,
                target_price  = 0.0,
                min_rr        = 0.0,
                size_mult     = 1.0,
                market_regime = regime.label,
                ticker_trend  = ticker_trend,
                reason        = f"regime flipped to {regime.trend} — exit held long",
            )

        # 2. momentum fade — see _momentum_fade_exit
        if exit_cfg.get("momentum_fade", True) and self._momentum_fade_exit(row, prev):
            return SignalResult(
                passed        = True,
                direction     = "exit_long",
                signal_type   = "momentum",
                stop_price    = 0.0,
                target_price  = 0.0,
                min_rr        = 0.0,
                size_mult     = 1.0,
                market_regime = regime.label,
                ticker_trend  = ticker_trend,
                reason        = "momentum fade — exit held long",
            )

        # 3. mean-reversion exit: overbought + macd_hist turning down
        if exit_cfg.get("mean_rev", True) and self._mean_rev_exit(row, prev):
            return SignalResult(
                passed        = True,
                direction     = "exit_long",
                signal_type   = "mean_reversion",
                stop_price    = 0.0,
                target_price  = 0.0,
                min_rr        = 0.0,
                size_mult     = 1.0,
                market_regime = regime.label,
                ticker_trend  = ticker_trend,
                reason        = "overbought + momentum down — exit held long",
            )

        return SignalResult(
            False,
            reason="no exit condition met (hold)",
            market_regime=regime.label, ticker_trend=ticker_trend,
        )

    # ── public — regime classifier (for scoring + main pipeline) ─────────────

    def market_regime(
            self,
            market_dfs: dict[str, pd.DataFrame] | None,
            vix_df: pd.DataFrame | None,
    ) -> MarketRegime:
        """
        Public wrapper around _market_regime.

        For callers that need regime classification standalone — e.g. main.py
        computing the regime once and passing it down to SignalScorer.enrich().
        """
        return self._market_regime(market_dfs, vix_df)
    # ── private — regime classifier ──────────────────────────────────────────

    def _market_regime(
        self,
        market_dfs: dict[str, pd.DataFrame] | None,
        vix_df:     pd.DataFrame | None,
    ) -> MarketRegime:
        """
        Classify the broad market on trend and volatility axes.

        Trend (regime.index_symbols, default SPY + QQQ vs each MA50):
            require_all_indices: true  → BULL if all > MA50,
                                         BEAR if all < MA50, else CHOP
            require_all_indices: false → majority vote among up/down

        Empty market_dfs defaults trend to BULL. Shorts still blocked
        because they require BEAR.

        Volatility (VIX close vs regime.vix_low / regime.vix_high):
            None vix_df defaults to NORMAL; high-vol cutoff disabled.

        Returns
        -------
        MarketRegime
        """
        rcfg = self._cfg.get("regime", {})

        # ── volatility ───────────────────────────────────────────────────────
        if vix_df is not None and not vix_df.empty:
            vix_close = float(vix_df["close"].iloc[-1])
            vix_low   = rcfg.get("vix_low",  20)
            vix_high  = rcfg.get("vix_high", 25)
            if   vix_close < vix_low:  volatility: VolState = "LOW"
            elif vix_close > vix_high: volatility = "HIGH"
            else:                      volatility = "NORMAL"
        else:
            volatility = "NORMAL"

        # ── trend ────────────────────────────────────────────────────────────
        if market_dfs is None or not market_dfs:
            return MarketRegime(trend="BULL", volatility=volatility)

        symbols     = rcfg.get("index_symbols", ["SPY", "QQQ"])
        require_all = rcfg.get("require_all_indices", True)
        ma_period   = self._cfg["trend"]["ma_fast"]

        votes_up = 0
        votes_dn = 0
        for sym in symbols:
            idx_df = market_dfs.get(sym)
            if idx_df is None or len(idx_df) < ma_period:
                continue
            ma   = idx_df["close"].rolling(ma_period, min_periods=ma_period).mean().iloc[-1]
            last = idx_df["close"].iloc[-1]
            if   last > ma: votes_up += 1
            elif last < ma: votes_dn += 1

        total_votes = votes_up + votes_dn
        if total_votes == 0:
            trend: TrendState = "BULL"
        elif require_all:
            if   votes_up == total_votes: trend = "BULL"
            elif votes_dn == total_votes: trend = "BEAR"
            else:                         trend = "CHOP"
        else:
            if   votes_up > votes_dn: trend = "BULL"
            elif votes_dn > votes_up: trend = "BEAR"
            else:                     trend = "CHOP"

        return MarketRegime(trend=trend, volatility=volatility)

    # ── private — ticker trend classifier ────────────────────────────────────

    def _ticker_trend(self, df: pd.DataFrame) -> TickerTrend:
        """
        Three-state ticker trend from MA50 / MA200 stack.

            UPTREND   close > MA50 > MA200
            DOWNTREND close < MA50 < MA200
            CHOP      anything else
        """
        fast = self._cfg["trend"]["ma_fast"]
        slow = self._cfg["trend"]["ma_slow"]

        close   = df["close"]
        ma_fast = close.rolling(fast, min_periods=fast).mean().iloc[-1]
        ma_slow = close.rolling(slow, min_periods=slow).mean().iloc[-1]
        last    = close.iloc[-1]

        if   last > ma_fast > ma_slow: return "UPTREND"
        elif last < ma_fast < ma_slow: return "DOWNTREND"
        else:                          return "CHOP"

    # ── private — entry evaluator (longs only) ───────────────────────────────

    def _evaluate_entry(
        self,
        row:          pd.Series,
        prev:         pd.Series,
        regime:       MarketRegime,
        ticker_trend: TickerTrend,
    ) -> tuple[Direction, SignalType, str]:
        """
        Evaluate long-entry conditions with regime and trend gating.

        Order (first match wins; momentum before mean-reversion):
            a. Momentum long  → trend == UPTREND
            b. Mean-rev long  → trend != DOWNTREND

        Returns (direction, signal_type, reason).
        """
        if regime.allows_longs:
            if ticker_trend == "UPTREND" and self._momentum_long(row, prev):
                return "long", "momentum", "momentum long"
            if ticker_trend != "DOWNTREND" and self._mean_rev_long(row, prev):
                return "long", "mean_reversion", "mean-reversion long"

        # No entry — explain why for the log
        if regime.volatility == "HIGH":
            return "none", "none", f"regime {regime.label}: high volatility blocks entries"
        if not regime.allows_longs:
            return "none", "none", f"regime {regime.label}: trend blocks long entries"
        return "none", "none", "no entry conditions met"

    # ── private — entry triggers ─────────────────────────────────────────────

    def _momentum_long(self, row: pd.Series, prev: pd.Series) -> bool:
        """
        Fires when macd_hist crossed above zero (prev < 0 < current) AND
        RSI in signals.momentum.long [rsi_min, rsi_max].
        """
        cfg = self._cfg["signals"]["momentum"]["long"]
        return (
            prev["macd_hist"] < 0 < row["macd_hist"]
            and cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]
        )

    def _mean_rev_long(self, row: pd.Series, prev: pd.Series) -> bool:
        """
        Fires when RSI < signals.mean_reversion.long.rsi_max AND
        macd_hist delta (row − prev) ≥ min_hist_delta.
        """
        cfg   = self._cfg["signals"]["mean_reversion"]["long"]
        delta = row["macd_hist"] - prev["macd_hist"]
        return row["rsi"] < cfg["rsi_max"] and delta >= cfg["min_hist_delta"]

    # ── private — exit triggers ──────────────────────────────────────────────

    def _momentum_fade_exit(self, row: pd.Series, prev: pd.Series) -> bool:
        """
        Held-long exit on momentum fade.

        Mirror of the momentum-short condition: macd_hist crossed below zero
        AND RSI in signals.momentum.short [rsi_min, rsi_max] (overbought-side
        band that confirms a directional turn). Configured under
        signals.momentum.short for reuse.
        """
        cfg = self._cfg["signals"]["momentum"]["short"]
        return (
            prev["macd_hist"] > 0 > row["macd_hist"]
            and cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]
        )

    def _mean_rev_exit(self, row: pd.Series, prev: pd.Series) -> bool:
        """
        Held-long exit on mean-reversion overbought.

        RSI > signals.mean_reversion.short.rsi_min AND macd_hist turning down
        by at least min_hist_delta — meaningful, not noise.
        """
        cfg   = self._cfg["signals"]["mean_reversion"]["short"]
        delta = row["macd_hist"] - prev["macd_hist"]
        return row["rsi"] > cfg["rsi_min"] and delta <= -cfg["min_hist_delta"]

    # ── private — small helpers ──────────────────────────────────────────────

    def _scan_pass_reason(
        self,
        df:   pd.DataFrame,
        row:  pd.Series,
        dv20: float,
    ) -> str:
        """
        Build a descriptive reason string for a passing scan result.

        Format: "UPTREND | vol×2.1 | RSI 54 | MACD↑ | 20d✓"

        trend     UPTREND/DOWNTREND/CHOP from MA50/MA200 stack
        vol_mult  today's volume / 20-day average volume
        rsi       RSI(14) on the last bar
        macd_dir  ↑ when macd_hist > 0, else ↓
        bkout_20d appended when close > prior 20-bar high
        """
        fast = self._cfg["trend"]["ma_fast"]
        slow = self._cfg["trend"]["ma_slow"]

        close = df["close"]
        last  = float(row["close"])

        # Strict min_periods matches _ticker_trend so the persisted reason
        # cannot disagree with the signal-gate trend on the same bar.
        if len(df) >= slow:
            ma_fast = close.rolling(fast, min_periods=fast).mean().iloc[-1]
            ma_slow = close.rolling(slow, min_periods=slow).mean().iloc[-1]
            if   last > ma_fast > ma_slow: trend = "UPTREND"
            elif last < ma_fast < ma_slow: trend = "DOWNTREND"
            else:                          trend = "CHOP"
        else:
            trend = "CHOP"

        # Volume multiplier vs 20-day average
        avg_vol  = float(df["volume"].tail(20).mean())
        vol_mult = float(row["volume"]) / avg_vol if avg_vol > 0 else 0.0

        # RSI
        rsi_val = float(row["rsi"]) if "rsi" in row.index else float("nan")

        # MACD histogram direction
        macd_dir = "↑" if ("macd_hist" in row.index and row["macd_hist"] > 0) else "↓"

        # 20-day breakout: close above the highest high of the prior 20 bars
        prior_high = float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else float("nan")
        bkout = " | 20d✓" if (not pd.isna(prior_high) and last > prior_high) else ""

        return (
            f"{trend} | vol×{vol_mult:.1f} | RSI {rsi_val:.0f} | MACD{macd_dir}{bkout}"
        )

    def _signal_blocked(self) -> tuple[bool, str]:
        """
        Check whether today appears in events.stop_dates.

        Returns
        -------
        (True, reason)  when signals should be suppressed.
        (False, "")     on a normal trading day.
        """
        today_str = self._today.isoformat()
        for entry in self._cfg.get("events", {}).get("stop_dates", []) or []:
            if entry["date"] == today_str:
                return True, (
                    f"stop date #{entry['id']}: {entry['description']} ({today_str})"
                )
        return False, ""

    def _near_earnings(self, earnings_date: date | None) -> bool:
        """
        True when earnings_date is within events.earnings_buffer_days of today.
        Returns False when earnings_date is None or in the past.
        """
        if earnings_date is None or earnings_date < self._today:
            return False
        buffer_days = self._cfg.get("events", {}).get("earnings_buffer_days", 5)
        return (earnings_date - self._today).days <= buffer_days

    @staticmethod
    def _rr_ok(entry: float, stop: float, min_rr: float, is_long: bool) -> bool:
        """
        Structural R:R sanity check.

        Long  target = entry + risk × min_rr is always positive for positive
              entry, so the only failure mode is risk == 0.
        Short target = entry − risk × min_rr must remain strictly positive,
              so risk × min_rr < entry.
        """
        risk = abs(entry - stop)
        if risk == 0:
            return False
        if is_long:
            return True
        return (risk * min_rr) < entry
