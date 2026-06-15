"""Typed engine config (core.config.parse).

Locks the parse contract: required keys + types are enforced (ConfigError),
optional keys fall back to core.defaults.DEFAULTS, present values win, and the
module stays a leaf (no import of the engine it configures).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import EngineConfig, parse
from exceptions import ConfigError


def _min_cfg() -> dict:
    """Smallest dict satisfying every key parse() requires (no defaults)."""
    return {
        "price": {"min_price": 5.0},
        "liquidity": {"min_dollar_volume_20d": 5_000_000},
        "market_cap": {"min_market_cap": 300_000_000},
        "volatility": {"min_atr_pct": 1.0, "max_atr_pct": 8.0},
        "trend": {"ma_fast": 50, "ma_slow": 200},
        "regime": {"ma_short": 20},
        "signals": {
            "stop_loss": {"atr_multiplier": 2.5, "min_rr": 2.5},
            "momentum": {
                "long": {"rsi_min": 50, "rsi_max": 70, "min_hist_delta_atr": 0.08},
                "short": {"rsi_min": 30, "rsi_max": 65, "min_hist_delta_atr": 0.18},
            },
            "mean_reversion": {
                "long": {"rsi_max": 30, "min_hist_delta_atr": 0.18},
                "short": {"rsi_min": 65, "min_hist_delta_atr": 0.05},
            },
        },
    }


def test_required_values_parsed():
    cfg = parse(_min_cfg())
    assert isinstance(cfg, EngineConfig)
    assert cfg.price.min_price == 5.0
    assert cfg.liquidity.min_dollar_volume_20d == 5_000_000
    assert cfg.market_cap.min_market_cap == 300_000_000
    assert (cfg.volatility.min_atr_pct, cfg.volatility.max_atr_pct) == (1.0, 8.0)
    assert (cfg.trend.ma_fast, cfg.trend.ma_slow) == (50, 200)
    assert (cfg.signals.stop_loss.atr_multiplier, cfg.signals.stop_loss.min_rr) == (2.5, 2.5)


def test_optional_defaults_from_registry():
    cfg = parse(_min_cfg())
    # absent → core.defaults.DEFAULTS / documented code fallbacks
    assert cfg.regime.vix_low == 20
    assert cfg.regime.vix_high == 28
    assert cfg.regime.require_all_indices is True
    assert cfg.execution.max_hold_days is None        # absent → off
    assert cfg.execution.max_hold_mode == "hard"
    assert cfg.execution.breakeven_trigger_r is None
    assert cfg.signals.stop_loss.min_rr_short is None
    assert cfg.raw is not None                         # source dict retained


def test_present_values_win_over_defaults():
    raw = _min_cfg()
    raw["regime"]["vix_low"] = 15
    raw["execution"] = {"max_hold_days": 25, "max_hold_mode": "if_not_profit",
                        "breakeven_trigger_r": 1.0}
    cfg = parse(raw)
    assert cfg.regime.vix_low == 15
    assert cfg.execution.max_hold_days == 25
    assert cfg.execution.max_hold_mode == "if_not_profit"
    assert cfg.execution.breakeven_trigger_r == 1.0


def test_missing_required_key_raises():
    raw = _min_cfg()
    del raw["trend"]["ma_slow"]
    with pytest.raises(ConfigError):
        parse(raw)


def test_wrong_type_raises():
    raw = _min_cfg()
    raw["trend"]["ma_slow"] = "200"     # str, not int
    with pytest.raises(ConfigError):
        parse(raw)
    raw = _min_cfg()
    raw["price"]["min_price"] = True    # bool is not a number here
    with pytest.raises(ConfigError):
        parse(raw)


def test_parses_real_filters_yaml():
    import yaml
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config" / "filters.yaml").read_text(encoding="utf-8"))
    cfg = parse(raw)
    # sanity against the shipped values
    assert cfg.trend.ma_slow == 200
    assert cfg.execution.breakeven_trigger_r == 1.0    # ADR-004 default ON
    assert cfg.signals.borrow.annual_rate_default == 0.03
    assert len(cfg.events.stop_dates) == 2             # shipped blackout calendar


def test_signal_legs_parsed():
    cfg = parse(_min_cfg())
    assert cfg.signals.momentum.long.rsi_min == 50
    assert cfg.signals.momentum.long.rsi_max == 70
    assert cfg.signals.momentum.long.max_bars_since_cross == 3   # DEFAULTS fallback
    assert cfg.signals.momentum.short.min_hist_delta_atr == 0.18
    assert cfg.signals.mean_reversion.long.rsi_max == 30
    assert cfg.signals.mean_reversion.short.rsi_min == 65
    # short_entry blocks absent → None (keeps those triggers disabled)
    assert cfg.signals.momentum.short_entry is None
    assert cfg.signals.mean_reversion.short_entry is None


def test_short_entry_leg_present():
    raw = _min_cfg()
    raw["signals"]["momentum"]["short_entry"] = {
        "rsi_min": 30, "rsi_max": 50, "min_hist_delta_atr": 0.08}
    cfg = parse(raw)
    assert cfg.signals.momentum.short_entry is not None
    assert cfg.signals.momentum.short_entry.rsi_max == 50
    assert cfg.signals.momentum.short_entry.max_bars_since_cross == 3


def test_toggles_and_events_defaults():
    cfg = parse(_min_cfg())
    s = cfg.signals
    assert s.gap_risk.enabled is False
    assert s.gap_risk.max_prev_bar_range_atr == 3.0
    assert s.sector_gate.enabled is False
    assert s.sector_gate.sector_map_path == "config/sector_map.yaml"
    assert s.size_mult_gate.min == 0.25
    # exit toggles all default True
    assert s.exits.regime_flip and s.exits.momentum_fade and s.exits.mean_rev
    assert s.exits.regime_flip_short and s.exits.short_cover_pop and s.exits.short_cover_oversold
    assert s.hard_to_borrow_list == []
    assert s.require_trigger_bar_up is False
    assert s.allow_shorts is False
    assert s.borrow.annual_rate_default == 0.0
    assert cfg.events.earnings_buffer_days == 5
    assert cfg.events.stop_dates == []


def test_toggle_present_values_win():
    raw = _min_cfg()
    raw["signals"]["gap_risk"] = {"enabled": True, "max_prev_bar_range_atr": 2.0}
    raw["signals"]["exits"] = {"regime_flip": False}   # others fall back to True
    raw["signals"]["hard_to_borrow_list"] = ["GME"]
    raw["events"] = {"earnings_buffer_days": 7}
    cfg = parse(raw)
    assert cfg.signals.gap_risk.enabled is True
    assert cfg.signals.gap_risk.max_prev_bar_range_atr == 2.0
    assert cfg.signals.exits.regime_flip is False
    assert cfg.signals.exits.momentum_fade is True
    assert cfg.signals.hard_to_borrow_list == ["GME"]
    assert cfg.events.earnings_buffer_days == 7


def test_config_module_is_a_leaf():
    import core.config as config_mod
    src = Path(config_mod.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]          # skip the module docstring
    assert "filter_engine" not in body
