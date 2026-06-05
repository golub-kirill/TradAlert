"""
Single source of truth for runtime defaults.

Previously many config keys had their default value duplicated as a Python
literal inside the consuming module (e.g. ``rcfg.get("vix_high", 25)``
while ``filters.yaml`` had ``vix_high: 28``). Removing a key from YAML
therefore *silently* changed behaviour.

All defaults now live here. Code calls ``DEFAULTS.get("...")``; if YAML is
missing the key, the documented value is applied. YAML values, when
present, win.
"""

from __future__ import annotations

from typing import Any

_VALUES: dict[str, Any] = {
    # filters.yaml
    "filters.regime.vix_low": 20,
    "filters.regime.vix_high": 28,
    "filters.regime.ma_short": 20,
    "filters.regime.require_ma_short_alignment": False,
    "filters.regime.require_all_indices": True,
    "filters.regime.index_symbols": ["SPY", "QQQ"],
    "filters.regime.vix_symbol": "^VIX",
    "filters.events.earnings_buffer_days": 5,
    "filters.signals.momentum.long.max_bars_since_cross": 3,
    "filters.signals.momentum.short_entry.max_bars_since_cross": 3,
    "filters.signals.gap_risk.enabled": False,
    "filters.signals.gap_risk.max_prev_bar_range_atr": 3.0,
    "filters.signals.size_mult_gate.min": 0.25,
    # settings.yaml
    "settings.storage.staleness_hours": 12,
    "settings.scanner.min_score_to_alert": 50,
    "settings.market_hours.expected_hold_days_low": 10,
    "settings.market_hours.expected_hold_days_high": 20,
    "settings.scanner.vbp.lookback": 120,
    "settings.scanner.vbp.n_bins": 24,
    "settings.scanner.vbp.volume_percentile": 70,
    "settings.scanner.chart.signal_history": True,
    "settings.behavioral.data_dir": "data/behavioral",
    "settings.behavioral.stale_window_days": 14,
    "settings.macro.size_mult_floor": 0.25,
    "settings.macro.size_mult_ceiling": 1.0,
    "settings.macro.fred_api_key_env": "FRED_API_KEY",
    "settings.behavioral.size_mult_floor": 0.25,
    "settings.behavioral.size_mult_ceiling": 1.0,
    "settings.behavioral.breadth_divergence_penalty": 0.2,
    "settings.fetcher.max_workers": 8,
}


class _Defaults:
    """Read-only registry of canonical defaults."""

    def get(self, dotted: str) -> Any:
        if dotted not in _VALUES:
            raise KeyError(
                f"No default registered for {dotted!r}. Either add one to "
                "core.defaults._VALUES or require the YAML to provide it."
            )
        return _VALUES[dotted]

    def get_or(self, dotted: str, fallback: Any) -> Any:
        return _VALUES.get(dotted, fallback)


DEFAULTS: _Defaults = _Defaults()
