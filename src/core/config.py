"""Typed, validated view of filters.yaml (the FilterEngine config).

Mirrors the nested filters.yaml structure as frozen dataclasses so engine code
reads ``cfg.trend.ma_slow`` instead of ``cfg["trend"]["ma_slow"]`` — required
keys and types are checked once in ``parse()`` (raising ConfigError) rather than
failing at scattered access sites. Defaults come from ``core.defaults.DEFAULTS``
so there is still ONE source of truth; YAML values win when present.

This is Phase 1 of the typed-config migration: the full tree is modelled here,
but only the engine's trend + scan-gate reads consume it so far (the rest still
read the raw dict via ``FilterEngine._cfg``). ``parse()`` accepts every config
that ``FilterEngine._validate_config`` accepts — it adds coverage and type
checks, never a new rejection of an otherwise-valid config. It is a pure
representation of the dict, so behaviour is unchanged.
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


# ── scan-gate blocks (beachhead — all required, see _REQUIRED_CONFIG_KEYS) ─────

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


# ── remaining blocks (modelled now; consumed in later migration phases) ───────

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
class EngineConfig:
    """Typed view of filters.yaml. ``raw`` retains the source dict for blocks
    not yet migrated off ``FilterEngine._cfg``."""
    price: PriceCfg
    liquidity: LiquidityCfg
    market_cap: MarketCapCfg
    volatility: VolatilityCfg
    trend: TrendCfg
    regime: RegimeCfg
    execution: ExecutionCfg
    stop_loss: StopLossCfg
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
        stop_loss=StopLossCfg(
            atr_multiplier=_num(cfg, "signals.stop_loss.atr_multiplier"),
            min_rr=_num(cfg, "signals.stop_loss.min_rr"),
            min_rr_short=_opt_num(cfg, "signals.stop_loss.min_rr_short")),
        raw=cfg,
    )
