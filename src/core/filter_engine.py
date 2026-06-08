"""
Two-stage filter pipeline.

    scan()    structural quality gate, always runs.
    signal()  long-entry detection (held_long=False) or exit detection on
              a held long (held_long=True).

Required indicator columns on the input DataFrame: rsi, atr, macd_hist.
The engine is stateless; construct once, call per ticker per bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml
from pandas import Series

from core.defaults import DEFAULTS
from exceptions import ConfigError, InsufficientDataError

logger = logging.getLogger(__name__)

# ── module-level helpers (config type checking) ──────────────────────────────

_NUMERIC: tuple[type, ...] = (int, float)


def _type_ok(value: object, expected: type | tuple[type, ...]) -> bool:
    """
    isinstance() with ``bool`` excluded from ``int``.

    YAML booleans deserialize to Python ``True``/``False``, subclasses of
    ``int``; without this guard ``ma_fast: true`` would silently pass an
    int type-check.
    """
    accept = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool):
        return bool in accept
    return isinstance(value, accept)


def _type_name(expected: type | tuple[type, ...]) -> str:
    """Format an expected-type spec for error messages."""
    if isinstance(expected, type):
        return expected.__name__
    return " or ".join(t.__name__ for t in expected)

# ── type aliases ──────────────────────────────────────────────────────────────

TrendState = Literal["BULL", "BEAR", "CHOP"]
VolState = Literal["LOW", "NORMAL", "HIGH"]
TickerTrend = Literal["UPTREND", "DOWNTREND", "CHOP"]
Direction = Literal["long", "short", "exit_long", "exit_short", "none"]
SignalType = Literal["momentum", "mean_reversion", "regime", "time_stop", "none"]


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class MarketRegime:
    """
    Two-axis classification of the broad market.

    Attributes
    ----------
    trend       : "BULL" | "BEAR" | "CHOP".
    volatility  : "LOW" | "NORMAL" | "HIGH". Defaults to NORMAL when vix_df is None.
    vix_rising  : True when VIX has risen over the slope-lookback window. Set
                  by ``_market_regime`` from ``regime.vix_slope_lookback_days``
                  (default 5 bars). Consulted by the entry gate when
                  ``regime.vix_slope_block`` is enabled — see the Feb 2025
    macro       : Optional MacroState (default None).
    behavioral  : Optional BehavioralState (default None).
    """
    trend: TrendState
    volatility: VolState
    vix_rising: bool = False
    macro: object | None = None
    behavioral: object | None = None

    @property
    def label(self) -> str:
        """Combined label, e.g. ``"BULL_LOW"``."""
        return f"{self.trend}_{self.volatility}"

    @property
    def allows_longs(self) -> bool:
        """True when trend is BULL and volatility is not HIGH."""
        return self.trend == "BULL" and self.volatility != "HIGH"

    @property
    def allows_shorts(self) -> bool:
        """True when trend is BEAR and volatility is not HIGH.

        Mirror image of ``allows_longs``. The HIGH-volatility carve-out
        applies symmetrically — chaotic markets are bad in both directions.
        """
        return self.trend == "BEAR" and self.volatility != "HIGH"

    @property
    def size_multiplier(self) -> float:
        """Composite position-size multiplier from macro × behavioral.

        Geometric mean preserves the property that if either axis says
        zero-risk, the composite goes to zero. Missing axes contribute 1.0
        (neutral). Result clamped to [0.0, 1.0].
        """
        m = getattr(self.macro, "size_multiplier", 1.0) if self.macro else 1.0
        b = getattr(self.behavioral, "size_multiplier", 1.0) if self.behavioral else 1.0
        try:
            m = float(m);
            b = float(b)
        except (TypeError, ValueError):
            return 1.0
        if m <= 0 or b <= 0:
            return 0.0
        composite = (m * b) ** 0.5  # geometric mean
        return max(0.0, min(1.0, composite))


@dataclass
class ScanResult:
    """
    Output of FilterEngine.scan().

    Attributes
    ----------
    passed       : True when all scan filters cleared.
    reason       : Explanation string; always populated.
    close        : Last-bar close price. None when scan raised before compute.
    atr          : ATR(14) on the last bar.
    atr_pct      : atr / close × 100.
    dv20         : 20-day average dollar volume.
    market_cap   : Market cap in dollars; None when not supplied.
    rsi          : RSI(14) on the last bar.
    macd         : MACD line on the last bar.
    macd_signal  : MACD signal line on the last bar.
    macd_hist    : MACD histogram on the last bar.
    """
    passed: bool
    reason: str = ""

    # ── last-bar snapshot (populated inside scan()) ──────────────────────────
    close: float | None = field(default=None, repr=False)
    atr: float | None = field(default=None, repr=False)
    atr_pct: float | None = field(default=None, repr=False)
    dv20: float | None = field(default=None, repr=False)
    market_cap: float | None = field(default=None, repr=False)
    rsi: float | None = field(default=None, repr=False)
    macd: float | None = field(default=None, repr=False)
    macd_signal: float | None = field(default=None, repr=False)
    macd_hist: float | None = field(default=None, repr=False)


@dataclass
class GateCheck:
    """
    One factor row in the entry-gate "trigger panel".

    A direction-aware, *post-decision* description of a single entry factor —
    the engine's "proof of opinion". Rendered factor-grouped on the chart
    sidebar and folded into the Telegram factor line from the same source, so
    the two surfaces can never disagree with the real decision.

    Building these never affects a decision (they are derived only after a
    signal has fired, behind ``signal(with_checks=True)``), so the backtest and
    sweep paths leave ``checks`` empty and replay bit-identically.

    Attributes
    ----------
    group    : Factor group for layout — "TREND" | "MOMENTUM" | "LOCATION" |
               "VOLATILITY" | "RISK" | "CONTEXT".
    name     : Short row label, e.g. "RSI", "MACD Δ", "R:R".
    passed   : Binary pass/fail; drives ✓/✗ and the per-group summary mark.
    detail   : Value text shown beside the mark, e.g. "62.3", "2.50×".
    strength : Optional grade in [0, 1] for continuous factors → rendered as a
               ●●●○ bar. None marks a hard binary (rendered ✓/✗).
    """
    group: str
    name: str
    passed: bool
    detail: str = ""
    strength: float | None = None


@dataclass
class SignalResult:
    """
    Output of FilterEngine.signal().

    Attributes
    ----------
    passed             : True when a signal fired and all gates cleared.
    direction          : "long" | "exit_long" | "none".
    signal_type        : "momentum" | "mean_reversion" | "regime" | "none".
                         "regime" pairs only with ``direction="exit_long"``.
    stop_price         : ``close − ATR × atr_multiplier`` on entry. 0.0 on exit.
    target_price       : ``close + risk × min_rr`` on entry. 0.0 on exit.
    min_rr             : Minimum risk:reward ratio from config. 0.0 on exit.
    size_mult          : Position-size multiplier ∈ [0, 1] from macro/behavioral
                         regime. 1.0 = full size, 0.5 = half size, 0.0 = block.
    market_regime      : Regime label at signal time, e.g. ``"BULL_NORMAL"``.
    ticker_trend       : "UPTREND" | "DOWNTREND" | "CHOP" | "N/A".
    reason             : Explanation string; always populated.
    score              : Confidence score in [0, 100]. 0.0 until enriched.
    score_components   : Sub-score dict ``{name: 0..1}``. Empty until enriched.
    timeframe          : "daily".
    expected_hold_days : (low, high) trading-day range, display-only. The live path
                         (main.py) sets it from the reference backtest's actual
                         bars_held p25-p75; this (10, 15) default is the fallback.
    watch_only         : True when triggered but ``score < min_score_to_alert``.
    description        : Multi-line detail block built by SignalScorer.
    """
    passed: bool
    direction: Direction = "none"
    signal_type: SignalType = "none"
    stop_price: float = 0.0
    target_price: float = 0.0
    min_rr: float = 0.0
    size_mult: float = 1.0
    market_regime: str = ""
    ticker_trend: str = ""
    reason: str = ""
    # ── enriched by SignalScorer ──────────────────────────────────────────────
    score: float = field(default=0.0, repr=False)
    score_components: dict[str, float] = field(default_factory=dict, repr=False)
    timeframe: str = field(default="daily", repr=False)
    expected_hold_days: tuple[int, int] = field(default=(10, 15), repr=False)
    watch_only: bool = field(default=False, repr=False)
    description: str = field(default="", repr=False)
    # ── entry-gate trigger panel (populated only when signal(with_checks=True)) ──
    checks: list[GateCheck] = field(default_factory=list, repr=False)


# ── engine ────────────────────────────────────────────────────────────────────

class FilterEngine:
    """
    Stateless two-stage filter engine. Construct once, call per ticker per bar.

    Parameters
    ----------
    config_path : Path to filters.yaml. Default "config/filters.yaml".
    today       : Override "today" for backtesting. Default date.today().
    """

    _REQUIRED_CONFIG_KEYS: tuple[tuple[str, type | tuple[type, ...]], ...] = (
        ("price.min_price", _NUMERIC),
        ("liquidity.min_dollar_volume_20d", _NUMERIC),
        ("market_cap.min_market_cap", _NUMERIC),
        ("volatility.min_atr_pct", _NUMERIC),
        ("volatility.max_atr_pct", _NUMERIC),
        ("trend.ma_fast", int),
        ("trend.ma_slow", int),
        ("regime.ma_short", int),
        ("regime.require_ma_short_alignment", bool),
        ("signals.momentum.long.rsi_min", _NUMERIC),
        ("signals.momentum.long.rsi_max", _NUMERIC),
        ("signals.momentum.long.min_hist_delta_atr", _NUMERIC),
        ("signals.momentum.short.rsi_min", _NUMERIC),
        ("signals.momentum.short.rsi_max", _NUMERIC),
        ("signals.momentum.short.min_hist_delta_atr", _NUMERIC),
        ("signals.mean_reversion.long.rsi_max", _NUMERIC),
        ("signals.mean_reversion.long.min_hist_delta_atr", _NUMERIC),
        ("signals.mean_reversion.short.rsi_min", _NUMERIC),
        ("signals.mean_reversion.short.min_hist_delta_atr", _NUMERIC),
        ("signals.stop_loss.atr_multiplier", _NUMERIC),
        ("signals.stop_loss.min_rr", _NUMERIC),
    )

    _EARNINGS_BUFFER_DAYS_DEFAULT: int = 5

    def __init__(
            self,
            config_path: Path | str = "config/filters.yaml",
            today: date | None = None,
    ):
        with open(config_path, encoding="utf-8") as f:
            self._cfg = yaml.safe_load(f)
        self._validate_config()
        self._today = today or date.today()
        self._stop_dates = self._build_stop_dates_index()
        self._sector_map = self._load_sector_map()

    # ── private — config validation ──────────────────────────────────────────

    def _validate_config(self) -> None:
        """
        Walk ``_REQUIRED_CONFIG_KEYS`` and validate presence + type.

        Raises
        ------
        ConfigError
            On the first missing dotted key or type mismatch encountered.
        """
        for dotted, expected in self._REQUIRED_CONFIG_KEYS:
            node = self._cfg
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    raise ConfigError(dotted, reason="missing")
                node = node[part]
            if not _type_ok(node, expected):
                raise ConfigError(
                    dotted,
                    reason=f"expected {_type_name(expected)}, got {type(node).__name__}",
                )

    def _build_stop_dates_index(self) -> dict[str, dict]:
        """
        Index ``events.stop_dates`` from filters.yaml by ISO date string.

        Returns
        -------
        dict[str, dict]
            ``{"YYYY-MM-DD": {"id": int, "date": str, "description": str}}``.

        Raises
        ------
        ConfigError
            When a stop-date entry is not a dict or lacks ``date``, ``id``, or
            ``description``.
        """
        raw = self._cfg.get("events", {}).get("stop_dates", []) or []
        index: dict[str, dict] = {}
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"events.stop_dates[{i}]",
                    reason=f"expected dict, got {type(entry).__name__}",
                )
            for required in ("date", "id", "description"):
                if required not in entry:
                    raise ConfigError(
                        f"events.stop_dates[{i}].{required}",
                        reason="missing",
                    )
            index[str(entry["date"])] = entry
        return index

    def _load_sector_map(self) -> dict[str, str | None]:
        sg = self._cfg.get("signals", {}).get("sector_gate", {})
        if not sg.get("enabled", False):
            return {}
        map_path = sg.get("sector_map_path", "config/sector_map.yaml")
        path = Path(map_path)
        if not path.exists():
            logger.warning("sector map not found at %s — sector gate disabled", path)
            return {}
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return dict(raw.get("sector_map", {}))

    # ── scan: liquidity & quality screen ───────────────────────────────────────────────────────────────

    def scan(
            self,
            ticker: str,
            df: pd.DataFrame,
            market_cap: float | None = None,
    ) -> ScanResult:
        """
        Structural quality check. Never blocked by stop_dates.

        Checks applied in order:
            1. Row count ≥ 20
            2. close ≥ ``price.min_price``
            3. 20-day avg dollar volume ≥ ``liquidity.min_dollar_volume_20d``
            4. ``market_cap`` ≥ ``market_cap.min_market_cap`` (skipped when None)
            5. ATR% within ``volatility.min_atr_pct`` … ``max_atr_pct``

        ATR% = ``atr / close × 100``.

        The last-bar metric snapshot is attached to every returned ScanResult,
        including failing ones.

        Parameters
        ----------
        ticker     : Symbol; used for logging only.
        df         : DataFrame with indicators already computed.
        market_cap : Market cap in dollars; None skips the gate.

        Returns
        -------
        ScanResult

        Raises
        ------
        InsufficientDataError   When ``len(df) < trend.ma_slow``.
        KeyError                When a required indicator column is missing.
        """
        min_rows = self._cfg["trend"]["ma_slow"]
        if len(df) < min_rows:
            raise InsufficientDataError(got=len(df), need=min_rows, ticker=ticker)

        row = df.iloc[-1]
        # NaN guard: warmup bars carry NaN indicators. Without this, gate
        # comparisons (e.g. ``atr_pct < min``) silently evaluate False and the
        # ticker passes. The live path is normally pre-guarded by
        # _indicators_ready, but scan() must be self-defending.
        if pd.isna(row["close"]) or pd.isna(row["atr"]):
            return ScanResult(
                passed=False, reason="indicators in warmup (NaN on last bar)",
            )

        dv20 = float((df["close"] * df["volume"]).tail(20).mean())

        # ── capture snapshot now so all exit paths can carry it ───────────────
        def _snapshot(reason: str, passed: bool) -> ScanResult:
            logger.debug(
                "scan %s %s: %s",
                "PASS" if passed else "FAIL", ticker, reason,
            )
            return ScanResult(
                passed=passed,
                reason=reason,
                close=float(row["close"]),
                atr=float(row["atr"]),
                atr_pct=float(row["atr"] / row["close"] * 100),
                dv20=dv20,
                market_cap=market_cap,
                rsi=float(row["rsi"]),
                macd=float(row["macd"]),
                macd_signal=float(row["macd_signal"]),
                macd_hist=float(row["macd_hist"]),
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

    # ── signal: entry / exit generation ───────────────────────────────────────────────────────────────

    def signal(
            self,
            ticker: str,
            df: pd.DataFrame,
            market_dfs: dict[str, pd.DataFrame] | None = None,
            vix_df: pd.DataFrame | None = None,
            earnings_date: date | None = None,
            held_long: bool = False,
            held_short: bool = False,
            regime: MarketRegime | None = None,
            with_checks: bool = False,
    ) -> SignalResult:
        """
        Signal detection. Branches on ``held_long``.

        held_long=False (entry mode). Long-entry detection with regime,
        trend, and earnings gating. Gate order:
            1. stop_date blackout
            2. row-count guard (≥ ``trend.ma_slow`` rows)
            3. earnings buffer
            4. entry condition (regime + trend + trigger)
            5. R:R sanity

        held_long=True (exit mode). Exit detection on a currently-held long.
        Skips gates 1 and 3 (stop-dates and earnings buffers protect new
        risk only). Row-count guard still applies. Exit fires on:
            • momentum fade  (macd_hist crosses below zero + RSI confirms)
            • mean-rev exit  (RSI overbought + macd_hist turning down)
            • regime flip    (regime no longer BULL)
        Exit signals fire under HIGH volatility.

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
            When ``len(df) < trend.ma_slow``.
        """
        # Regime is computed in both modes so it can be reported.
        # If a pre-computed regime is provided, use it instead of recomputing.
        if regime is None:
            regime = self._market_regime(market_dfs, vix_df)

        result = (
            self._signal_exit_short(ticker, df, regime)
            if held_short
            else self._signal_exit(ticker, df, regime)
            if held_long
            else self._signal_entry(
                ticker, df, regime, earnings_date, market_dfs,
                with_checks=with_checks,
            )
        )

        if result.passed:
            logger.info(
                "signal FIRED %s %s/%s: %s",
                ticker, result.direction, result.signal_type, result.reason,
            )
        else:
            logger.debug("signal NONE %s: %s", ticker, result.reason)
        return result

    # ── entry mode ──────────────────────────────────────────────────

    def _signal_entry(
            self,
            ticker: str,
            df: pd.DataFrame,
            regime: MarketRegime,
            earnings_date: date | None,
            market_dfs: dict[str, pd.DataFrame] | None = None,
            with_checks: bool = False,
    ) -> SignalResult:
        """Long-entry signal detection with full gate chain.

        ``with_checks`` only adds the post-decision trigger-panel ``checks`` to a
        fired result; it never changes a decision, so the backtest/sweep path
        (``with_checks=False``) replays bit-identically and pays no extra compute.
        """
        # 1. stop_date blackout
        blocked, reason = self._signal_blocked()
        if blocked:
            return self._fail_result(reason, regime, "N/A")

        # 2. row-count guard
        self._min_rows_guard(df, ticker)

        ticker_trend = self._ticker_trend(df)

        # 3. earnings buffer
        if self._near_earnings(earnings_date):
            buf = self._earnings_buffer_days()
            days_to = (earnings_date - self._today).days
            return self._fail_result(
                f"earnings in {days_to}d (buffer {buf}d)",
                regime, ticker_trend,
            )

        row = df.iloc[-1]
        prev = df.iloc[-2]

        # 4b. gap risk filter
        gr = self._cfg.get("signals", {}).get("gap_risk", {})
        if gr.get("enabled", False):
            max_range = gr.get("max_prev_bar_range_atr",
                                DEFAULTS.get("filters.signals.gap_risk.max_prev_bar_range_atr"))
            prev_range = prev["high"] - prev["low"]
            if prev_range > max_range * prev["atr"]:
                return self._fail_result(
                    f"prev bar range {prev_range:.2f} > {max_range:.1f}*ATR ({prev['atr']:.2f})",
                    regime, ticker_trend,
                )

        # 4c. sector-relative strength gate
        sg = self._cfg.get("signals", {}).get("sector_gate", {})
        if sg.get("enabled", False):
            ok, reason = self._sector_strength_ok(ticker, market_dfs)
            if not ok:
                return self._fail_result(reason, regime, ticker_trend)

        # 5. evaluate long-entry conditions
        direction, signal_type, why = self._evaluate_entry(
            row, prev, df, regime, ticker_trend,
        )

        if direction == "none":
            return self._fail_result(why, regime, ticker_trend)

        # Hard-to-borrow gate (shorts only, opt-in).
        # Many small caps are HTB / unavailable to borrow; v1 assumed
        # availability. Symbols listed in ``signals.hard_to_borrow_list``
        # cannot be shorted. No effect on longs or when the list is absent.
        if direction == "short":
            htb = self._cfg.get("signals", {}).get("hard_to_borrow_list", []) or []
            if ticker in set(htb):
                return self._fail_result(
                    f"{ticker} on hard-to-borrow list; short entry blocked",
                    regime, ticker_trend,
                )

        # 5a. Anti-gap entry confirmation (opt-in).
        # Postmortem 2026-05-27 found 11 of 36 stop-outs failed within 3
        # bars (-12.9R, 26% of all stop damage). All fired on red bars
        # (close < open). Require trigger-bar close ≥ open before queuing
        # the T+1 entry. Cheap gate, no cost when off.
        if self._cfg.get("signals", {}).get("require_trigger_bar_up", False):
            try:
                tr_close = float(row["close"])
                tr_open = float(row["open"])
            except (KeyError, TypeError, ValueError):
                tr_close = tr_open = 0.0
            if tr_close < tr_open:
                return self._fail_result(
                    f"trigger bar red (close {tr_close:.2f} < open {tr_open:.2f}); "
                    "anti-gap gate blocks entry",
                    regime, ticker_trend,
                )

        # 6. R:R sanity — branch on direction.
        atr_mult = self._cfg["signals"]["stop_loss"]["atr_multiplier"]
        min_rr = self._cfg["signals"]["stop_loss"]["min_rr"]
        is_long_dir = (direction == "long")
        # Shorts have a bounded upside (price floor of $0), so
        # they can warrant a tighter reward ceiling. ``min_rr_short`` lets
        # the short side demand a different R:R. Absent → falls back to
        # ``min_rr`` so longs and pre-v2 configs are unchanged.
        if not is_long_dir:
            min_rr = self._cfg["signals"]["stop_loss"].get("min_rr_short", min_rr)
        stop_dist = row["atr"] * atr_mult
        if is_long_dir:
            stop_price = row["close"] - stop_dist
            target_price = row["close"] + stop_dist * min_rr
        else:
            # Short: stop above entry, target below.
            stop_price = row["close"] + stop_dist
            target_price = row["close"] - stop_dist * min_rr

        if not self._rr_ok(row["close"], stop_price, min_rr, is_long=is_long_dir):
            return self._fail_result(
                f"R:R below minimum {min_rr}", regime, ticker_trend,
            )

        # size_mult_gate: block entries when the composite macro x behavioral
        # position-size multiplier is below the configured floor. Default off
        # (enabled=false) so the long-only baseline replays bit-identically.
        smg = self._cfg.get("signals", {}).get("size_mult_gate", {})
        if smg.get("enabled", False):
            min_mult = float(smg.get("min", DEFAULTS.get("filters.signals.size_mult_gate.min")))
            if regime.size_multiplier < min_mult:
                return self._fail_result(
                    f"size_mult {regime.size_multiplier:.2f} < gate min {min_mult:.2f}",
                    regime, ticker_trend,
                )

        checks = (
            self._build_gate_checks(
                row, prev, df, regime, ticker_trend, direction, signal_type,
                stop_price, target_price, min_rr, atr_mult, earnings_date,
                market_dfs,
            )
            if with_checks else []
        )

        return SignalResult(
            passed=True,
            direction=direction,
            signal_type=signal_type,
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            min_rr=min_rr,
            size_mult=round(regime.size_multiplier, 4),
            market_regime=regime.label,
            ticker_trend=ticker_trend,
            reason="entry signal fired",
            checks=checks,
        )

    # ── entry-gate trigger panel (post-decision; never alters a decision) ─────

    def _build_gate_checks(
            self,
            row: Series,
            prev: Series,
            df: pd.DataFrame,
            regime: MarketRegime,
            ticker_trend: TickerTrend,
            direction: Direction,
            signal_type: SignalType,
            stop_price: float,
            target_price: float,
            min_rr: float,
            atr_mult: float,
            earnings_date: date | None,
            market_dfs: dict[str, pd.DataFrame] | None,
    ) -> list[GateCheck]:
        """
        Re-derive a direction-aware, factor-grouped read of *why this signal
        fired*, for the chart sidebar and the Telegram factor line.

        Called only after a signal has fired (``with_checks=True``). It reads
        the same config thresholds and already-computed indicators the decision
        used, so the panel reflects the real gates; it changes nothing. Every
        factor is computed defensively — a missing column or short history skips
        that row rather than raising on the live path.

        Semantics flip by ``direction``: strength, "clear path", and regime
        tailwind/headwind all invert long ↔ short.
        """
        checks: list[GateCheck] = []
        is_long = direction == "long"
        sgn = 1.0 if is_long else -1.0

        def add(group: str, name: str, passed: bool,
                detail: str = "", strength: float | None = None) -> None:
            checks.append(GateCheck(
                group=group, name=name, passed=bool(passed),
                detail=detail, strength=strength,
            ))

        def clamp01(x: float) -> float:
            return max(0.0, min(1.0, float(x)))

        def f(key: str) -> float | None:
            try:
                v = row[key]
            except (KeyError, IndexError):
                return None
            return float(v) if pd.notna(v) else None

        close = f("close")
        atr = f("atr")
        rsi = f("rsi")
        hist = f("macd_hist")
        prev_hist = float(prev["macd_hist"]) if pd.notna(prev.get("macd_hist")) else None
        ma_fast = f("ma_fast")
        ma_slow = f("ma_slow")

        # ── TREND ────────────────────────────────────────────────────────────
        ideal_trend = "UPTREND" if is_long else "DOWNTREND"
        add("TREND", "Trend", ticker_trend == ideal_trend, ticker_trend)
        if close is not None and ma_fast is not None:
            add("TREND", "Px vs MA50",
                (close > ma_fast) if is_long else (close < ma_fast),
                f"{close:.2f}/{ma_fast:.2f}")
        if close is not None and ma_slow is not None:
            add("TREND", "Px vs MA200",
                (close > ma_slow) if is_long else (close < ma_slow),
                f"{ma_slow:.2f}")
        if "ma_fast" in df.columns and len(df) >= 11:
            mf_prev = df["ma_fast"].iloc[-11]
            if pd.notna(mf_prev) and ma_fast is not None and float(mf_prev) != 0:
                slope = (ma_fast - float(mf_prev)) / abs(float(mf_prev)) * 100
                add("TREND", "MA50 slope",
                    (slope > 0) if is_long else (slope < 0), f"{slope:+.1f}%")
        wk = f("weekly_sma10")
        if close is not None and wk is not None:
            add("TREND", "Weekly",
                (close > wk) if is_long else (close < wk), f"{wk:.2f}")

        # ── MOMENTUM ─────────────────────────────────────────────────────────
        sig = self._cfg["signals"]
        if signal_type == "momentum":
            tcfg = sig["momentum"]["long"] if is_long else (sig["momentum"].get("short_entry") or {})
        else:
            tcfg = sig["mean_reversion"]["long"] if is_long else (sig["mean_reversion"].get("short_entry") or {})

        if rsi is not None:
            rsi_min = tcfg.get("rsi_min")
            rsi_max = tcfg.get("rsi_max")
            if rsi_min is not None and rsi_max is not None:
                in_band = rsi_min <= rsi <= rsi_max
                strength = clamp01((rsi - rsi_min) / (rsi_max - rsi_min)) if rsi_max > rsi_min else None
                add("MOMENTUM", "RSI", in_band, f"{rsi:.1f} [{rsi_min:g}-{rsi_max:g}]", strength)
            elif rsi_max is not None:  # mean-rev long: oversold gate
                add("MOMENTUM", "RSI", rsi < rsi_max, f"{rsi:.1f} <{rsi_max:g}",
                    clamp01((rsi_max - rsi) / rsi_max))
            elif rsi_min is not None:  # mean-rev short: overbought gate
                add("MOMENTUM", "RSI", rsi > rsi_min, f"{rsi:.1f} >{rsi_min:g}",
                    clamp01((rsi - rsi_min) / max(1.0, 100 - rsi_min)))

        if hist is not None:
            add("MOMENTUM", "MACD hist",
                (hist > 0) if is_long else (hist < 0), f"{hist:+.3f}")
        if hist is not None and prev_hist is not None and atr is not None:
            delta = hist - prev_hist
            thr = float(tcfg.get("min_hist_delta_atr", 0.0)) * atr
            passed = (delta >= thr) if is_long else (delta <= -thr)
            strength = clamp01(sgn * delta / thr) if thr > 0 else None
            add("MOMENTUM", "MACD Δ", passed, f"{delta:+.3f}/{thr:.3f}", strength)

        if signal_type == "momentum" and "macd_hist" in df.columns:
            max_bars = int(tcfg.get(
                "max_bars_since_cross",
                DEFAULTS.get("filters.signals.momentum.long.max_bars_since_cross")))
            h = df["macd_hist"].iloc[-(max_bars + 3):]
            bars_ago = None
            for i in range(len(h) - 2, -1, -1):
                up = h.iloc[i] < 0 <= h.iloc[i + 1]
                down = h.iloc[i] >= 0 > h.iloc[i + 1]
                if (up if is_long else down):
                    bars_ago = len(h) - 2 - i
                    break
            if bars_ago is not None:
                add("MOMENTUM", "Fresh cross", bars_ago <= max_bars,
                    f"{bars_ago}b ≤{max_bars}")

        # ── LOCATION & STRENGTH ──────────────────────────────────────────────
        window = df.tail(252)
        if close is not None and len(window) >= 60:
            hi = float(window["high"].max())
            lo = float(window["low"].min())
            if hi > lo:
                pos = (close - lo) / (hi - lo)
                strength = clamp01(pos if is_long else 1 - pos)
                add("LOCATION", "52W pos", strength >= 0.5, f"{pos * 100:.0f}%", strength)

        spy = (market_dfs or {}).get("SPY")
        if close is not None and spy is not None and len(spy) >= 61 and len(df) >= 61:
            base_t = float(df["close"].iloc[-61])
            base_s = float(spy["close"].iloc[-61])
            if base_t > 0 and base_s > 0:
                rs = (close / base_t - float(spy["close"].iloc[-1]) / base_s) * 100
                add("LOCATION", "RS vs SPY",
                    (rs > 0) if is_long else (rs < 0), f"{rs:+.1f}%",
                    clamp01(0.5 + sgn * rs / 40))

        if close is not None and atr is not None and atr > 0:
            try:
                from core.indicators.vbp import (
                    compute_vbp, nearest_high_volume_node_above,
                    nearest_high_volume_node_below,
                )
                vbp = compute_vbp(df)
                node = (nearest_high_volume_node_above(vbp, close) if is_long
                        else nearest_high_volume_node_below(vbp, close))
                if node is None:
                    add("LOCATION", "Clear path", True, "clear")
                else:
                    np_price = node[0]
                    clear = (np_price >= target_price) if is_long else (np_price <= target_price)
                    dist = abs(np_price - close) / atr
                    add("LOCATION", "Clear path", clear, f"{dist:.1f} ATR")
            except Exception as exc:  # VBP is best-effort context, never fatal
                logger.debug("trigger-panel VBP failed: %s", exc)

        # ── VOLATILITY ───────────────────────────────────────────────────────
        bw = f("bb_bw")
        if bw is not None and "bb_bw" in df.columns:
            series = df["bb_bw"].tail(120).dropna()
            if len(series) >= 20:
                pctile = float((series < bw).mean()) * 100
                add("VOLATILITY", "BB %ile", pctile <= 50.0,
                    f"{pctile:.0f}%ile", clamp01(1 - pctile / 100))
        bbz = f("bb_z")
        if bbz is not None:
            add("VOLATILITY", "BB z", abs(bbz) < 2.0, f"{bbz:+.2f}")
        if close is not None and atr is not None and close > 0:
            atr_pct = atr / close * 100
            min_atr = self._cfg["volatility"]["min_atr_pct"]
            max_atr = self._cfg["volatility"]["max_atr_pct"]
            add("VOLATILITY", "ATR%", min_atr <= atr_pct <= max_atr, f"{atr_pct:.1f}%")

        # ── RISK ─────────────────────────────────────────────────────────────
        add("RISK", "R:R", True, f"{float(min_rr):.2f}")
        if close is not None and atr is not None and close > 0:
            stop_dist = atr * atr_mult
            add("RISK", "Stop", True, f"{stop_dist / close * 100:.1f}% / {atr_mult:.1f}ATR")
        # Position-size multiplier (macro × behavioral) — the validated portfolio
        # sizes each entry by this, so the live read must show it (NORTH STAR).
        smult = clamp01(float(regime.size_multiplier))
        add("RISK", "Size", smult >= 0.5, f"{smult:.2f}x", strength=smult)
        if earnings_date is not None and earnings_date >= self._today:
            days = (earnings_date - self._today).days
            add("RISK", "Earnings", days > self._earnings_buffer_days(), f"{days}d")
        else:
            add("RISK", "Earnings", True, "—")
        if not is_long:
            # A fired short already cleared the hard-to-borrow gate, so it is
            # borrowable; surface the borrow drag that will be charged.
            rate = float(sig.get("borrow", {}).get("annual_rate_default", 0.0)) * 100
            add("RISK", "Borrow", True, f"{rate:.1f}%/yr")

        # ── CONTEXT (engine portion; main.py adds RP / budget / health) ──────
        macro = getattr(regime, "macro", None)
        risk_on = getattr(macro, "risk_on_score", None) if macro is not None else None
        if risk_on is not None:
            tail = (risk_on > 0.5) == is_long
            add("CONTEXT", "Regime", tail, f"{regime.label} risk-on {risk_on:.2f}")
        else:
            add("CONTEXT", "Regime", regime.allows_longs if is_long else regime.allows_shorts,
                regime.label)
        if "volume" in df.columns and close is not None:
            dv20 = float((df["close"] * df["volume"]).tail(20).mean())
            add("CONTEXT", "Liquidity", True, f"${dv20 / 1e6:.1f}M")

        return checks

    # ── exit mode ───────────────────────────────────────────────────

    def _signal_exit(
            self,
            ticker: str,
            df: pd.DataFrame,
            regime: MarketRegime,
    ) -> SignalResult:
        """
        Exit-signal detection for held longs.

        Skips stop_date blackout and earnings buffer; runs under HIGH volatility.
        Fires on the first matching condition:
            1. regime flip      — regime no longer BULL
            2. momentum fade    — macd_hist crosses below zero + RSI confirms
            3. mean-rev exit    — RSI overbought + macd_hist turning down

        Each condition is individually toggleable via
        ``signals.exits.<name>`` in filters.yaml (all default True).
        """
        # row-count guard still applies — trend label needs MA200
        self._min_rows_guard(df, ticker)

        ticker_trend = self._ticker_trend(df)
        row = df.iloc[-1]
        prev = df.iloc[-2]

        exit_cfg = self._cfg.get("signals", {}).get("exits", {})

        # 1. regime flip — any non-BULL regime triggers exit on a held long
        if exit_cfg.get("regime_flip", True) and regime.trend != "BULL":
            return self._exit_result(
                "regime",
                f"regime flipped to {regime.trend} — exit held long",
                regime, ticker_trend,
            )

        # 2. momentum fade — see _momentum_fade_exit
        if exit_cfg.get("momentum_fade", True) and self._momentum_fade_exit(row, prev):
            return self._exit_result(
                "momentum",
                "momentum fade — exit held long",
                regime, ticker_trend,
            )

        # 3. mean-reversion exit: overbought + macd_hist turning down
        if exit_cfg.get("mean_rev", True) and self._mean_rev_exit(row, prev):
            return self._exit_result(
                "mean_reversion",
                "overbought + momentum down — exit held long",
                regime, ticker_trend,
            )

        return self._fail_result(
            "no exit condition met (hold)", regime, ticker_trend,
        )

    # ── public — regime classifier (for scoring + main pipeline) ─────────────

    def market_regime(
            self,
            market_dfs: dict[str, pd.DataFrame] | None,
            vix_df: pd.DataFrame | None,
    ) -> MarketRegime:
        """Public wrapper around ``_market_regime`` for standalone callers."""
        return self._market_regime(market_dfs, vix_df)

    # ── private — regime classifier ──────────────────────────────────────────

    def _market_regime(
            self,
            market_dfs: dict[str, pd.DataFrame] | None,
            vix_df: pd.DataFrame | None,
    ) -> MarketRegime:
        """
        Classify the broad market on trend and volatility.

        Trend
            ``regime.index_symbols`` (default ``[SPY, QQQ]``) vs each
            ``MA(trend.ma_fast)``. With ``require_all_indices=true``: BULL
            iff all > MA, BEAR iff all < MA, else CHOP. Otherwise majority
            vote. Empty/missing ``market_dfs`` → trend defaults to BULL.

        Volatility
            VIX close vs ``regime.vix_low`` / ``regime.vix_high``. None
            ``vix_df`` → defaults to NORMAL.

        Returns
        -------
        MarketRegime
        """
        rcfg = self._cfg.get("regime", {})

        # ── volatility ───────────────────────────────────────────────────────
        volatility: VolState
        vix_rising = False
        if vix_df is not None and not vix_df.empty:
            vix_close = float(vix_df["close"].iloc[-1])
            vix_low = rcfg.get("vix_low", DEFAULTS.get("filters.regime.vix_low"))
            vix_high = rcfg.get("vix_high", DEFAULTS.get("filters.regime.vix_high"))
            if vix_close < vix_low:
                volatility = "LOW"
            elif vix_close > vix_high:
                volatility = "HIGH"
            else:
                volatility = "NORMAL"

            # ── slope ──────────────────────────────────────────────────────
            # Compare today's VIX close to the close ``lookback`` bars ago.
            # Set unconditionally; the entry gate decides whether to act on
            # it via ``regime.vix_slope_block``. Defensive on short series.
            lookback = int(rcfg.get("vix_slope_lookback_days", 5))
            if len(vix_df) > lookback:
                vix_ref = float(vix_df["close"].iloc[-1 - lookback])
                vix_rising = vix_close > vix_ref
        else:
            volatility = "NORMAL"

        # ── trend ────────────────────────────────────────────────────────────
        if market_dfs is None or not market_dfs:
            # Default to CHOP (not BULL) when SPY/QQQ caches are missing, so
            # allows_longs == False and the entry gate blocks new entries
            # until data is restored. Logged at ERROR so it's visible in ops.
            logger.error(
                "market_regime: no index data supplied — defaulting to CHOP "
                "to block new entries."
            )
            return MarketRegime(trend="CHOP", volatility=volatility, vix_rising=vix_rising)

        symbols = rcfg.get("index_symbols", ["SPY", "QQQ"])
        require_all = rcfg.get("require_all_indices", True)
        ma_period = self._cfg["trend"]["ma_fast"]

        votes_up = 0
        votes_dn = 0
        for sym in symbols:
            idx_df = market_dfs.get(sym)
            if idx_df is None or len(idx_df) < ma_period:
                continue
            ma = idx_df["close"].iloc[-ma_period:].mean()
            last = idx_df["close"].iloc[-1]
            if last > ma:
                votes_up += 1
            elif last < ma:
                votes_dn += 1

        total_votes = votes_up + votes_dn
        trend: TrendState
        if total_votes == 0:
            trend = "BULL"
        elif require_all:
            if votes_up == total_votes:
                trend = "BULL"
            elif votes_dn == total_votes:
                trend = "BEAR"
            else:
                trend = "CHOP"
        else:
            if votes_up > votes_dn:
                trend = "BULL"
            elif votes_dn > votes_up:
                trend = "BEAR"
            else:
                trend = "CHOP"

        # Secondary short-term MA alignment gate
        if trend == "BULL":
            ma_short_ok = rcfg.get("require_ma_short_alignment", False)
            if ma_short_ok:
                ma_short = rcfg.get("ma_short", DEFAULTS.get("filters.regime.ma_short"))
                for sym in symbols:
                    idx_df = market_dfs.get(sym)
                    if idx_df is None or len(idx_df) < ma_short:
                        continue
                    ma_s = idx_df["close"].iloc[-ma_short:].mean()
                    if idx_df["close"].iloc[-1] < ma_s:
                        trend = "CHOP"
                        break

        return MarketRegime(trend=trend, volatility=volatility, vix_rising=vix_rising)

    # ── private — ticker trend classifier ────────────────────────────────────

    def _ticker_trend(self, df: pd.DataFrame) -> TickerTrend:
        """
        Three-state ticker trend from MA(trend.ma_fast)/MA(trend.ma_slow):
            UPTREND   close > MA_fast > MA_slow
            DOWNTREND close < MA_fast < MA_slow
            CHOP      anything else
        """
        fast = self._cfg["trend"]["ma_fast"]
        slow = self._cfg["trend"]["ma_slow"]
        # Fast path: read precomputed MA columns (attach_indicators uses the
        # same 50/200 periods the engine configures) — O(1) vs O(n) per bar.
        if "ma_fast" in df.columns and "ma_slow" in df.columns and len(df) >= slow:
            mf = df["ma_fast"].iloc[-1]
            ms = df["ma_slow"].iloc[-1]
            if pd.notna(mf) and pd.notna(ms):
                last = float(df["close"].iloc[-1])
                if last > float(mf) > float(ms):
                    return "UPTREND"
                if last < float(mf) < float(ms):
                    return "DOWNTREND"
                return "CHOP"
        return self._classify_trend(df["close"], fast, slow)

    def _sector_strength_ok(self, ticker, market_dfs):
        sector = self._sector_map.get(ticker)
        if sector is None:
            return True, ""
        if market_dfs is None or sector not in market_dfs:
            return True, ""
        sector_df = market_dfs[sector]
        if len(sector_df) < self._cfg["trend"]["ma_fast"]:
            return True, ""
        fast = self._cfg["trend"]["ma_fast"]
        ma = sector_df["close"].iloc[-fast:].mean()
        last = sector_df["close"].iloc[-1]
        if last < ma:
            return False, f"sector {sector} below MA({fast}) ({last:.2f} < {ma:.2f})"
        return True, ""

    # ── private — entry evaluator (longs only) ───────────────────────────────

    def _evaluate_entry(
            self,
            row: Series,
            prev: Series,
            df: pd.DataFrame,
            regime: MarketRegime,
            ticker_trend: TickerTrend,
    ) -> tuple[Direction, SignalType, str]:
        """
        Evaluate long-entry conditions with regime and trend gating.

        Order (first match wins):
            a. Momentum long  — requires ``ticker_trend == UPTREND``.
            b. Mean-rev long  — requires ``ticker_trend != DOWNTREND``.

        Returns ``(direction, signal_type, reason)``.
        """
        # ── VIX slope gate (opt-in) ──────────────────────────────────────
        # When ``regime.vix_slope_block`` is enabled and VIX has risen over
        # the configured lookback window, block fresh momentum entries even
        # if the absolute VIX level is still in the LOW or NORMAL band.
        # Targets the Feb 2025 cluster: 8 momentum trades / 1 win on bars
        # where VIX was below vix_low but climbing into a tariff scare.
        # Mean-reversion entries are NOT gated — they often want falling
        # markets / chop and have their own ATR-relative gates.
        rcfg = self._cfg.get("regime", {})
        slope_block = bool(rcfg.get("vix_slope_block", False))

        scfg_top = self._cfg.get("signals", {})
        allow_shorts = bool(scfg_top.get("allow_shorts", False))

        if regime.allows_longs:
            if ticker_trend == "UPTREND" and self._momentum_long(row, prev, df):
                if slope_block and regime.vix_rising:
                    return (
                        "none", "none",
                        "VIX slope-up: momentum blocked even at low VIX",
                    )
                return "long", "momentum", "momentum long"
            if ticker_trend != "DOWNTREND" and self._mean_rev_long(row, prev):
                return "long", "mean_reversion", "mean-reversion long"

        # Short-side entries. Only fire when the master
        # ``signals.allow_shorts`` switch is on AND the regime is
        # BEAR + not HIGH-vol. The ticker-trend gate mirrors the long
        # case: DOWNTREND for momentum shorts, !UPTREND for MR shorts.
        if allow_shorts and regime.allows_shorts:
            if ticker_trend == "DOWNTREND" and self._momentum_short_entry(row, prev, df):
                return "short", "momentum", "momentum short"
            if ticker_trend != "UPTREND" and self._mean_rev_short_entry(row, prev):
                return "short", "mean_reversion", "mean-reversion short"

        # No entry — explain why for the log
        if regime.volatility == "HIGH":
            return "none", "none", f"regime {regime.label}: high volatility blocks entries"
        if not regime.allows_longs and not (allow_shorts and regime.allows_shorts):
            return "none", "none", f"regime {regime.label}: trend blocks entries (longs and shorts)"
        if not regime.allows_longs:
            return "none", "none", f"regime {regime.label}: trend blocks long entries"
        return "none", "none", "no entry conditions met"

    # ── private — entry triggers ─────────────────────────────────────────────

    def _momentum_long(self, row: Series, prev: Series, df: pd.DataFrame) -> bool:
        cfg = self._cfg["signals"]["momentum"]["long"]
        max_bars = cfg.get("max_bars_since_cross",
                           DEFAULTS.get("filters.signals.momentum.long.max_bars_since_cross"))

        if row["macd_hist"] <= 0:
            return False
        if not (cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]):
            return False

        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        if delta < threshold:
            return False

        hist = df["macd_hist"].iloc[-(max_bars + 3):]
        for i in range(len(hist) - 2, -1, -1):
            if hist.iloc[i] < 0 <= hist.iloc[i + 1]:
                bars_ago = len(hist) - 2 - i
                return bars_ago <= max_bars

        return False

    def _mean_rev_long(self, row: Series, prev: Series) -> bool:
        """
        Mean-reversion long entry trigger.

        Fires when ``RSI < signals.mean_reversion.long.rsi_max`` AND
        ``macd_hist[row] - macd_hist[prev] >= min_hist_delta_atr * row["atr"]``.
        """
        cfg = self._cfg["signals"]["mean_reversion"]["long"]
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return row["rsi"] < cfg["rsi_max"] and delta >= threshold

    def _momentum_short_entry(self, row: Series, prev: Series,
                              df: pd.DataFrame) -> bool:
        """
        Fresh short-entry trigger on downside momentum.

        Mirror image of ``_momentum_long``:
          - ``macd_hist`` is currently negative
          - histogram has fallen by at least ``min_hist_delta_atr * atr``
            since the previous bar
          - RSI is in the configured ``short_entry`` band
          - a recent zero-cross DOWN happened within ``max_bars_since_cross``

        Config block: ``signals.momentum.short_entry``.
        """
        cfg = self._cfg["signals"]["momentum"].get("short_entry")
        if not cfg:
            return False
        max_bars = cfg.get("max_bars_since_cross",
                           DEFAULTS.get("filters.signals.momentum.short_entry.max_bars_since_cross"))

        if row["macd_hist"] >= 0:
            return False
        if not (cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]):
            return False

        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        # Symmetric to _momentum_long's ``delta < threshold``: short
        # requires ``delta <= -threshold`` (strong downward histogram move).
        if delta > -threshold:
            return False

        hist = df["macd_hist"].iloc[-(max_bars + 3):]
        for i in range(len(hist) - 2, -1, -1):
            # Recent zero-cross DOWN: hist[i] >= 0 > hist[i+1]
            if hist.iloc[i] >= 0 > hist.iloc[i + 1]:
                bars_ago = len(hist) - 2 - i
                return bars_ago <= max_bars

        return False

    def _mean_rev_short_entry(self, row: Series, prev: Series) -> bool:
        """
        Counter-trend short entry into a rally (mirror of mean_rev_long).

        Fires when ``RSI > signals.mean_reversion.short_entry.rsi_min`` AND
        histogram delta <= -``min_hist_delta_atr * atr`` (downtick).

        Note: ``mean_reversion.short`` is the *held-long exit* trigger and
        is NOT what we want here. The new ``mean_reversion.short_entry``
        block is the fresh-short trigger.
        """
        cfg = self._cfg["signals"]["mean_reversion"].get("short_entry")
        if not cfg:
            return False
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return row["rsi"] > cfg["rsi_min"] and delta <= -threshold

    # ── private — exit triggers ──────────────────────────────────────────────

    def _momentum_fade_exit(self, row: Series, prev: Series) -> bool:
        """
        Held-long exit on momentum fade.

        Fires when all hold:
            - ``prev["macd_hist"] > 0 > row["macd_hist"]``   (zero-crossing down)
            - ``rsi_min <= row["rsi"] <= rsi_max``           (RSI band)
            - ``row["macd_hist"] - prev["macd_hist"] <= -min_hist_delta_atr * row["atr"]``
              (magnitude gate: the fade must be a real drop, not noise
              dipping below zero by a hair)

        Config keys live under ``signals.momentum.short``.
        """
        cfg = self._cfg["signals"]["momentum"]["short"]
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return (
                prev["macd_hist"] > 0 > row["macd_hist"]
                and cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]
                and delta <= -threshold
        )

    def _mean_rev_exit(self, row: Series, prev: Series) -> bool:
        """
        Held-long exit on overbought mean-reversion.

        Fires when ``RSI > signals.mean_reversion.short.rsi_min`` AND
        ``macd_hist[row] - macd_hist[prev] <= -min_hist_delta_atr * row["atr"]``.
        """
        cfg = self._cfg["signals"]["mean_reversion"]["short"]
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return row["rsi"] > cfg["rsi_min"] and delta <= -threshold

    def _momentum_pop_exit(self, row: Series, prev: Series) -> bool:
        """
        Held-short exit on momentum pop.

        Mirror of ``_momentum_fade_exit``. Fires when:
          - ``prev["macd_hist"] < 0 < row["macd_hist"]``  (zero-cross UP)
          - RSI in the ``momentum.long`` band
          - histogram delta >= ``min_hist_delta_atr * atr``

        Re-uses the ``momentum.long`` config block — the trigger that
        would *open* a long is exactly what closes a short.
        """
        cfg = self._cfg["signals"]["momentum"]["long"]
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return (
                prev["macd_hist"] < 0 < row["macd_hist"]
                and cfg["rsi_min"] <= row["rsi"] <= cfg["rsi_max"]
                and delta >= threshold
        )

    def _mean_rev_short_cover(self, row: Series, prev: Series) -> bool:
        """
        Held-short exit on extreme oversold + upturn.

        Mirror of ``_mean_rev_exit``. Fires when RSI < ``mean_reversion.long.rsi_max``
        AND histogram delta >= ``min_hist_delta_atr * atr`` (signal turning up).
        Re-uses the ``mean_reversion.long`` config block for the same
        reason as ``_momentum_pop_exit``.
        """
        cfg = self._cfg["signals"]["mean_reversion"]["long"]
        delta = row["macd_hist"] - prev["macd_hist"]
        threshold = cfg["min_hist_delta_atr"] * row["atr"]
        return row["rsi"] < cfg["rsi_max"] and delta >= threshold

    def _signal_exit_short(
            self,
            ticker: str,
            df: pd.DataFrame,
            regime: MarketRegime,
    ) -> SignalResult:
        """
        Held-short exit detection.

        Mirror of ``_signal_exit``. Fires on:
          1. regime flip — any non-BEAR trend
          2. momentum pop — macd_hist crosses up + RSI confirms
          3. oversold cover — RSI low + macd_hist turning up

        Each is toggleable via ``signals.exits.<name>`` with new short
        keys defaulting to True.
        """
        self._min_rows_guard(df, ticker)
        ticker_trend = self._ticker_trend(df)
        row = df.iloc[-1]
        prev = df.iloc[-2]
        exit_cfg = self._cfg.get("signals", {}).get("exits", {})

        if exit_cfg.get("regime_flip_short", True) and regime.trend != "BEAR":
            return self._exit_short_result(
                "regime",
                f"regime flipped to {regime.trend} — cover held short",
                regime, ticker_trend,
            )
        if exit_cfg.get("short_cover_pop", True) and self._momentum_pop_exit(row, prev):
            return self._exit_short_result(
                "momentum",
                "momentum pop — cover held short",
                regime, ticker_trend,
            )
        if exit_cfg.get("short_cover_oversold", True) and self._mean_rev_short_cover(row, prev):
            return self._exit_short_result(
                "mean_reversion",
                "oversold + momentum up — cover held short",
                regime, ticker_trend,
            )

        return self._fail_result(
            "no exit condition met (hold short)", regime, ticker_trend,
        )

    def _exit_short_result(
            self,
            signal_type: SignalType,
            reason: str,
            regime: MarketRegime,
            ticker_trend: TickerTrend,
    ) -> SignalResult:
        """Build an ``exit_short`` SignalResult. Mirror of ``_exit_result``."""
        return SignalResult(
            passed=True,
            direction="exit_short",
            signal_type=signal_type,
            stop_price=0.0,
            target_price=0.0,
            min_rr=0.0,
            size_mult=1.0,
            market_regime=regime.label,
            ticker_trend=ticker_trend,
            reason=reason,
        )

    # ── private — shared helpers ─────────────────────────────────────────────

    def _min_rows_guard(self, df: pd.DataFrame, ticker: str) -> None:
        """
        Enforce minimum row count for signal evaluation.

        Need at least ``trend.ma_slow`` rows (for the MA stack) and at
        least 2 rows (so ``iloc[-2]`` exists).

        Raises
        ------
        InsufficientDataError
        """
        min_rows = max(2, self._cfg["trend"]["ma_slow"])
        if len(df) < min_rows:
            raise InsufficientDataError(got=len(df), need=min_rows, ticker=ticker)

    @staticmethod
    def _classify_trend(close: Series, fast: int, slow: int) -> TickerTrend:
        """
        Three-state trend from MA(fast)/MA(slow) on a close Series.

            UPTREND   close > MA(fast) > MA(slow)
            DOWNTREND close < MA(fast) < MA(slow)
            CHOP      anything else, or ``len(close) < slow``
        """
        if len(close) < slow:
            return "CHOP"
        ma_fast = close.iloc[-fast:].mean()
        ma_slow = close.iloc[-slow:].mean()
        last = close.iloc[-1]
        if last > ma_fast > ma_slow:
            return "UPTREND"
        elif last < ma_fast < ma_slow:
            return "DOWNTREND"
        else:
            return "CHOP"

    @staticmethod
    def _exit_result(
            signal_type: SignalType,
            reason: str,
            regime: MarketRegime,
            ticker_trend: TickerTrend,
    ) -> SignalResult:
        """Build a passing exit SignalResult. ``stop_price``, ``target_price``, ``min_rr`` all 0.0."""
        return SignalResult(
            passed=True,
            direction="exit_long",
            signal_type=signal_type,
            stop_price=0.0,
            target_price=0.0,
            min_rr=0.0,
            size_mult=1.0,
            market_regime=regime.label,
            ticker_trend=ticker_trend,
            reason=reason,
        )

    @staticmethod
    def _fail_result(
            reason: str,
            regime: MarketRegime,
            ticker_trend: str,
    ) -> SignalResult:
        """Build a non-passing SignalResult for any gate failure."""
        return SignalResult(
            passed=False,
            reason=reason,
            market_regime=regime.label,
            ticker_trend=ticker_trend,
        )

    def _scan_pass_reason(
            self,
            df: pd.DataFrame,
            row: Series,
            dv20: float,
    ) -> str:
        """
        Build a one-line reason string for a passing scan result.

        Format: ``"UPTREND | vol×2.1 | RSI 54 | MACD↑ | 20d✓"``.
        ``20d✓`` is appended only when ``close`` exceeds the prior 20-bar high.
        """
        fast = self._cfg["trend"]["ma_fast"]
        slow = self._cfg["trend"]["ma_slow"]

        last = float(row["close"])
        trend = self._classify_trend(df["close"], fast, slow)

        avg_vol = float(df["volume"].tail(20).mean())
        vol_mult = float(row["volume"]) / avg_vol if avg_vol > 0 else 0.0

        rsi_val = float(row["rsi"])
        macd_dir = "↑" if row["macd_hist"] > 0 else "↓"

        prior_high = float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else float("nan")
        bkout = " | 20d✓" if (not pd.isna(prior_high) and last > prior_high) else ""

        return (
            f"{trend} | vol×{vol_mult:.1f} | RSI {rsi_val:.0f} | MACD{macd_dir}{bkout}"
        )

    def _signal_blocked(self) -> tuple[bool, str]:
        """
        Check today against the pre-built stop_dates index.

        Returns
        -------
        (True, reason)  when signals should be suppressed.
        (False, "")     on a normal trading day.
        """
        today_str = self._today.isoformat()
        entry = self._stop_dates.get(today_str)
        if entry is None:
            return False, ""
        return True, (
            f"stop date #{entry['id']}: {entry['description']} ({today_str})"
        )

    def _near_earnings(self, earnings_date: date | None) -> bool:
        """
        True when ``earnings_date`` is within ``events.earnings_buffer_days``
        of today. Returns False when ``earnings_date`` is None or already past.
        """
        if earnings_date is None or earnings_date < self._today:
            return False
        return (earnings_date - self._today).days <= self._earnings_buffer_days()

    def _earnings_buffer_days(self) -> int:
        """Return ``events.earnings_buffer_days``, falling back to the class default."""
        return int(
            self._cfg.get("events", {}).get(
                "earnings_buffer_days", self._EARNINGS_BUFFER_DAYS_DEFAULT
            )
        )

    @staticmethod
    def _rr_ok(entry: float, stop: float, min_rr: float, is_long: bool) -> bool:
        """
        Structural R:R sanity check.

        Long  -- always valid when risk != 0; target is derived from min_rr so
                 the ratio is structurally guaranteed.
        Short -- additionally requires ``risk * min_rr < entry`` so the target
                 price stays positive.
        """
        risk = abs(entry - stop)
        if risk == 0:
            return False
        if is_long:
            return True
        return (risk * min_rr) < entry

    # ---- dict-based constructor (used by sweep engine) ----------------------

    @classmethod
    def from_dict(cls, cfg: dict, today: date | None = None) -> "FilterEngine":
        """
        Construct a FilterEngine directly from a config dict.

        Bypasses filesystem I/O entirely -- the sweep engine supplies a
        deep-copied, mutated copy of the base config for each parameter
        combination without writing a temp file.

        Parameters
        ----------
        cfg   : Full filters.yaml structure as a nested dict.
        today : Override ``_today``; defaults to ``date.today()``.
        """
        import copy
        obj = object.__new__(cls)
        obj._cfg = copy.deepcopy(cfg)
        obj._today = today or date.today()
        obj._validate_config()
        obj._stop_dates = obj._build_stop_dates_index()
        obj._sector_map = obj._load_sector_map()
        return obj
