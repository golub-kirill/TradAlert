"""``filters.regime.vix_symbol`` must reach the code that loads the series.

The key was parsed into ``RegimeCfg`` and registered in ``DEFAULTS`` but had zero
consumers — ``^VIX`` was hardcoded in every path, so editing the knob changed
nothing. The live paths now read it (``backtest/loader.py`` keeps its literal for
replay byte-identity, the same carve-out ``index_symbols`` has).
"""

from __future__ import annotations

import yaml

import telegram_bot
from core.defaults import DEFAULTS


def test_default_matches_the_module_fallback():
    assert DEFAULTS.get("filters.regime.vix_symbol") == "^VIX"
    assert telegram_bot._VIX_SYMBOL == "^VIX"


def test_engine_config_parses_the_knob():
    from core.config import parse
    from tests.test_config import _min_cfg

    cfg = _min_cfg()
    cfg["regime"]["vix_symbol"] = "^VIX9D"
    assert parse(cfg).regime.vix_symbol == "^VIX9D"


def test_daemon_reads_the_configured_symbol(tmp_path, monkeypatch):
    filters = tmp_path / "filters.yaml"
    filters.write_text(yaml.safe_dump({"regime": {"vix_symbol": "^VIX9D"}}),
                       encoding="utf-8")
    monkeypatch.setattr(telegram_bot, "FILTERS_YAML", filters)
    assert telegram_bot._vix_symbol() == "^VIX9D"


def test_daemon_falls_back_when_the_knob_is_absent(tmp_path, monkeypatch):
    filters = tmp_path / "filters.yaml"
    filters.write_text(yaml.safe_dump({"regime": {}}), encoding="utf-8")
    monkeypatch.setattr(telegram_bot, "FILTERS_YAML", filters)
    assert telegram_bot._vix_symbol() == "^VIX"


def test_daemon_vix_symbol_is_fail_open(tmp_path, monkeypatch):
    """An unreadable config must degrade to the fallback, not break the daemon."""
    monkeypatch.setattr(telegram_bot, "FILTERS_YAML", tmp_path / "missing.yaml")
    assert telegram_bot._vix_symbol() == "^VIX"


def test_shipped_config_still_declares_the_symbol():
    """The knob is live now — a silent removal would change what gets loaded."""
    cfg = yaml.safe_load(
        (telegram_bot.FILTERS_YAML).read_text(encoding="utf-8")) or {}
    assert (cfg.get("regime") or {}).get("vix_symbol") == "^VIX"
