"""Typed, validated view of filters.yaml (the FilterEngine config).

Mirrors the nested filters.yaml structure as frozen dataclasses so engine code
reads ``cfg.trend.ma_slow`` instead of ``cfg["trend"]["ma_slow"]`` — required
keys and types are checked once in ``parse()`` (raising ConfigError) rather than
failing at scattered access sites. Defaults come from ``core.defaults.DEFAULTS``
so there is still ONE source of truth; YAML values win when present.

``parse()`` is a pure representation of the validated dict — it adds coverage and
type checks but never rejects an otherwise-valid config, so behaviour is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.defaults import DEFAULTS
from exceptions import ConfigError

_MISSING = object()
_NUM = (int, float)


def _node(cfg: dict, dotted: str, default=_MISSING):
    """Walk a dotted path; return the value, ``default``, or raise if required."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise ConfigError(dotted, reason="missing")
            return default
        node = node[part]
    return node


def _num(cfg: dict, dotted: str, default=_MISSING) -> float:
    v = _node(cfg, dotted, default)
    if isinstance(v, bool) or not isinstance(v, _NUM):
        raise ConfigError(dotted, reason=f"expected number, got {type(v).__name__}")
    return v


def _int(cfg: dict, dotted: str, default=_MISSING) -> int:
    v = _node(cfg, dotted, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(dotted, reason=f"expected int, got {type(v).__name__}")
    return v


def _bool(cfg: dict, dotted: str, default) -> bool:
    v = _node(cfg, dotted, default)
    if not isinstance(v, bool):
        raise ConfigError(dotted, reason=f"expected bool, got {type(v).__name__}")
    return v


def _opt_num(cfg: dict, dotted: str):
    """Optional numeric: None when absent/None, validated when present."""
    v = _node(cfg, dotted, None)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, _NUM):
        raise ConfigError(dotted, reason=f"expected number or null, got {type(v).__name__}")
    return v


def _leg_or_none(cfg: dict, base: str, *, rsi_min: bool, rsi_max: bool,
                 delta: bool, max_bars_default: str | None) -> "SignalLeg | None":
    """Parse an OPTIONAL signal leg (e.g. ``signals.momentum.short_entry``).

    Returns None when the block is absent or empty — mirroring the engine's
    ``cfg = ....get("short_entry"); if not cfg: return False`` guard, so an
    absent block keeps that trigger disabled. When present, the flagged fields
    are required (the engine indexes them directly today)."""
    block = _node(cfg, base, None)
    if not block:
        return None
    return SignalLeg(
        rsi_min=_num(cfg, f"{base}.rsi_min") if rsi_min else None,
        rsi_max=_num(cfg, f"{base}.rsi_max") if rsi_max else None,
        min_hist_delta_atr=_num(cfg, f"{base}.min_hist_delta_atr") if delta else None,
        max_bars_since_cross=(_int(cfg, f"{base}.max_bars_since_cross",
                                   DEFAULTS.get_or(max_bars_default, 3))
                              if max_bars_default else None),
    )


# ── scan-gate blocks (all required, see _REQUIRED_CONFIG_KEYS) ─────────────────

@dataclass(frozen=True)
class PriceCfg:
    min_price: float


@dataclass(frozen=True)
class LiquidityCfg:
    min_dollar_volume_20d: float


@dataclass(frozen=True)
class MarketCapCfg:
    min_market_cap: float


@dataclass(frozen=True)
class VolatilityCfg:
    min_atr_pct: float
    max_atr_pct: float


@dataclass(frozen=True)
class TrendCfg:
    ma_fast: int
    ma_slow: int


# ── remaining config blocks ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RegimeCfg:
    index_symbols: list
    vix_symbol: str
    vix_low: float
    vix_high: float
    require_all_indices: bool
    ma_short: int
    require_ma_short_alignment: bool
    vix_slope_block: bool
    vix_slope_lookback_days: int


@dataclass(frozen=True)
class ExecutionCfg:
    entry_slippage_pct: float
    commission_r: float
    max_hold_days: int | None
    max_hold_mode: str
    breakeven_trigger_r: float | None
    breakeven_buffer_atr: float | None


@dataclass(frozen=True)
class EventsCfg:
    earnings_buffer_days: int
    # Raw [{id, date, description}] list; FilterEngine._build_stop_dates_index
    # validates + indexes it at construction.
    stop_dates: list


@dataclass(frozen=True)
class SignalLeg:
    """One entry/exit trigger leg. Fields absent for a given leg stay None."""
    rsi_min: float | None = None
    rsi_max: float | None = None
    min_hist_delta_atr: float | None = None
    max_bars_since_cross: int | None = None


@dataclass(frozen=True)
class StopLossCfg:
    atr_multiplier: float
    min_rr: float
    min_rr_short: float | None


@dataclass(frozen=True)
class GapRiskCfg:
    enabled: bool
    max_prev_bar_range_atr: float


@dataclass(frozen=True)
class SectorGateCfg:
    enabled: bool
    sector_map_path: str


@dataclass(frozen=True)
class OverextensionCfg:
    enabled: bool
    bb_z_max: float


@dataclass(frozen=True)
class PeadCfg:
    enabled: bool
    min_priors: int
    tercile_pct: float


@dataclass(frozen=True)
class ExitsCfg:
    regime_flip: bool
    momentum_fade: bool
    mean_rev: bool
    regime_flip_short: bool
    short_cover_pop: bool
    short_cover_oversold: bool
    # Held-long regime-flip exit shaping (A/B levers; defaults reproduce the
    # original "exit on any non-BULL bar" behavior byte-for-byte):
    #   regime_flip_bear_only  — True: exit only on BEAR (CHOP no longer exits).
    #   regime_flip_confirm_bars — N>1: require the flip to persist N consecutive
    #                              bars before exiting (1 = fire immediately).
    regime_flip_bear_only: bool = False
    regime_flip_confirm_bars: int = 1


@dataclass(frozen=True)
class BorrowCfg:
    annual_rate_default: float
    per_ticker: dict


@dataclass(frozen=True)
class MomentumCfg:
    long: SignalLeg
    short: SignalLeg            # held-long fade EXIT (legacy name)
    short_entry: SignalLeg | None


@dataclass(frozen=True)
class MeanReversionCfg:
    long: SignalLeg
    short: SignalLeg            # held-long overbought EXIT
    short_entry: SignalLeg | None


@dataclass(frozen=True)
class SignalsCfg:
    momentum: MomentumCfg
    mean_reversion: MeanReversionCfg
    stop_loss: StopLossCfg
    gap_risk: GapRiskCfg
    sector_gate: SectorGateCfg
    overextension: OverextensionCfg
    pead: PeadCfg
    exits: ExitsCfg
    hard_to_borrow_list: list
    require_trigger_bar_up: bool
    allow_shorts: bool
    borrow: BorrowCfg


@dataclass(frozen=True)
class EngineConfig:
    """Typed view of filters.yaml; ``raw`` retains the source dict for callers
    that still need raw access."""
    price: PriceCfg
    liquidity: LiquidityCfg
    market_cap: MarketCapCfg
    volatility: VolatilityCfg
    trend: TrendCfg
    regime: RegimeCfg
    execution: ExecutionCfg
    events: EventsCfg
    signals: SignalsCfg
    raw: dict = field(repr=False, default_factory=dict)


def parse(cfg: dict) -> EngineConfig:
    """Build the typed EngineConfig from a filters.yaml dict.

    Required keys (the scan gates, trend, regime MA, stop-loss R:R) must be
    present and correctly typed — same contract as FilterEngine._validate_config.
    Optional keys fall back to core.defaults.DEFAULTS. Unknown keys are ignored
    (forward-compatible). Raises ConfigError on a missing required key or a type
    mismatch.
    """
    D = DEFAULTS.get_or
    return EngineConfig(
        price=PriceCfg(min_price=_num(cfg, "price.min_price")),
        liquidity=LiquidityCfg(
            min_dollar_volume_20d=_num(cfg, "liquidity.min_dollar_volume_20d")),
        market_cap=MarketCapCfg(min_market_cap=_num(cfg, "market_cap.min_market_cap")),
        volatility=VolatilityCfg(
            min_atr_pct=_num(cfg, "volatility.min_atr_pct"),
            max_atr_pct=_num(cfg, "volatility.max_atr_pct")),
        trend=TrendCfg(
            ma_fast=_int(cfg, "trend.ma_fast"),
            ma_slow=_int(cfg, "trend.ma_slow")),
        regime=RegimeCfg(
            index_symbols=_node(cfg, "regime.index_symbols",
                                D("filters.regime.index_symbols", ["SPY", "QQQ"])),
            vix_symbol=_node(cfg, "regime.vix_symbol",
                             D("filters.regime.vix_symbol", "^VIX")),
            vix_low=_num(cfg, "regime.vix_low", D("filters.regime.vix_low", 20)),
            vix_high=_num(cfg, "regime.vix_high", D("filters.regime.vix_high", 28)),
            require_all_indices=_bool(cfg, "regime.require_all_indices",
                                      D("filters.regime.require_all_indices", True)),
            ma_short=_int(cfg, "regime.ma_short"),
            require_ma_short_alignment=_bool(cfg, "regime.require_ma_short_alignment",
                                             D("filters.regime.require_ma_short_alignment", False)),
            vix_slope_block=_bool(cfg, "regime.vix_slope_block", False),
            vix_slope_lookback_days=_int(cfg, "regime.vix_slope_lookback_days", 5)),
        execution=ExecutionCfg(
            entry_slippage_pct=_num(cfg, "execution.entry_slippage_pct", 0.0),
            commission_r=_num(cfg, "execution.commission_r", 0.0),
            max_hold_days=(None if _node(cfg, "execution.max_hold_days", None) is None
                           else _int(cfg, "execution.max_hold_days")),
            max_hold_mode=_node(cfg, "execution.max_hold_mode", "hard"),
            breakeven_trigger_r=_opt_num(cfg, "execution.breakeven_trigger_r"),
            breakeven_buffer_atr=_opt_num(cfg, "execution.breakeven_buffer_atr")),
        events=EventsCfg(
            earnings_buffer_days=_int(cfg, "events.earnings_buffer_days",
                                      D("filters.events.earnings_buffer_days", 5)),
            stop_dates=_node(cfg, "events.stop_dates", []) or []),
        signals=SignalsCfg(
            momentum=MomentumCfg(
                long=SignalLeg(
                    rsi_min=_num(cfg, "signals.momentum.long.rsi_min"),
                    rsi_max=_num(cfg, "signals.momentum.long.rsi_max"),
                    min_hist_delta_atr=_num(cfg, "signals.momentum.long.min_hist_delta_atr"),
                    max_bars_since_cross=_int(
                        cfg, "signals.momentum.long.max_bars_since_cross",
                        D("filters.signals.momentum.long.max_bars_since_cross", 3))),
                short=SignalLeg(
                    rsi_min=_num(cfg, "signals.momentum.short.rsi_min"),
                    rsi_max=_num(cfg, "signals.momentum.short.rsi_max"),
                    min_hist_delta_atr=_num(cfg, "signals.momentum.short.min_hist_delta_atr")),
                short_entry=_leg_or_none(
                    cfg, "signals.momentum.short_entry",
                    rsi_min=True, rsi_max=True, delta=True,
                    max_bars_default="filters.signals.momentum.short_entry.max_bars_since_cross")),
            mean_reversion=MeanReversionCfg(
                long=SignalLeg(
                    rsi_max=_num(cfg, "signals.mean_reversion.long.rsi_max"),
                    min_hist_delta_atr=_num(cfg, "signals.mean_reversion.long.min_hist_delta_atr")),
                short=SignalLeg(
                    rsi_min=_num(cfg, "signals.mean_reversion.short.rsi_min"),
                    min_hist_delta_atr=_num(cfg, "signals.mean_reversion.short.min_hist_delta_atr")),
                short_entry=_leg_or_none(
                    cfg, "signals.mean_reversion.short_entry",
                    rsi_min=True, rsi_max=False, delta=True, max_bars_default=None)),
            stop_loss=StopLossCfg(
                atr_multiplier=_num(cfg, "signals.stop_loss.atr_multiplier"),
                min_rr=_num(cfg, "signals.stop_loss.min_rr"),
                min_rr_short=_opt_num(cfg, "signals.stop_loss.min_rr_short")),
            gap_risk=GapRiskCfg(
                enabled=_bool(cfg, "signals.gap_risk.enabled",
                              D("filters.signals.gap_risk.enabled", False)),
                max_prev_bar_range_atr=_num(
                    cfg, "signals.gap_risk.max_prev_bar_range_atr",
                    D("filters.signals.gap_risk.max_prev_bar_range_atr", 3.0))),
            sector_gate=SectorGateCfg(
                enabled=_bool(cfg, "signals.sector_gate.enabled", False),
                sector_map_path=_node(cfg, "signals.sector_gate.sector_map_path",
                                      "config/sector_map.yaml")),
            overextension=OverextensionCfg(
                enabled=_bool(cfg, "signals.overextension.enabled", False),
                bb_z_max=_num(cfg, "signals.overextension.bb_z_max",
                              D("filters.signals.overextension.bb_z_max", 2.5))),
            pead=PeadCfg(
                enabled=_bool(cfg, "signals.pead.enabled",
                              D("filters.signals.pead.enabled", False)),
                min_priors=_int(cfg, "signals.pead.min_priors",
                                D("filters.signals.pead.min_priors", 8)),
                tercile_pct=_num(cfg, "signals.pead.tercile_pct",
                                 D("filters.signals.pead.tercile_pct", 0.667))),
            exits=ExitsCfg(
                regime_flip=_bool(cfg, "signals.exits.regime_flip", True),
                momentum_fade=_bool(cfg, "signals.exits.momentum_fade", True),
                mean_rev=_bool(cfg, "signals.exits.mean_rev", True),
                regime_flip_short=_bool(cfg, "signals.exits.regime_flip_short", True),
                short_cover_pop=_bool(cfg, "signals.exits.short_cover_pop", True),
                short_cover_oversold=_bool(cfg, "signals.exits.short_cover_oversold", True),
                regime_flip_bear_only=_bool(cfg, "signals.exits.regime_flip_bear_only", False),
                regime_flip_confirm_bars=_int(cfg, "signals.exits.regime_flip_confirm_bars", 1)),
            hard_to_borrow_list=_node(cfg, "signals.hard_to_borrow_list", []) or [],
            require_trigger_bar_up=_bool(cfg, "signals.require_trigger_bar_up", False),
            allow_shorts=_bool(cfg, "signals.allow_shorts", False),
            borrow=BorrowCfg(
                annual_rate_default=_num(cfg, "signals.borrow.annual_rate_default", 0.0),
                per_ticker=_node(cfg, "signals.borrow.per_ticker", {}) or {})),
        raw=cfg,
    )
