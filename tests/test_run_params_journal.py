"""A run's config snapshot must record the knobs the run actually moved.

Portfolio overrides (--max-open-risk, --trail-atr-mult, --breakeven-trigger-r,
--max-hold-days …) reach the engine through PortfolioConfig, never through the
YAML tree. The snapshot was a plain copy of that tree, so a tuned run journalled
the SHIPPED value for every one of them: the panel's PARAMS column saw no diff
and counted only the date window, and config_mismatch — which reference_run uses
to pick the expectancy baseline — called the run a match for the live strategy.
"""

from __future__ import annotations

import copy
import json

import pytest

from backtest import run_backtest as rb
from backtest.db import _flatten, config_mismatch

BASE_CFG = {
    "execution": {"entry_slippage_pct": 0.002, "exit_slippage_pct": 0.002,
                  "commission_r": 0.005, "max_hold_days": 25,
                  "max_hold_mode": "if_not_profit", "breakeven_trigger_r": 1.0},
    "signals": {"require_trigger_bar_up": True, "allow_shorts": False},
    "regime": {"vix_slope_block": False},
    "chronic_loser_penalty": {"enabled": True, "lookback_days": 90},
}


def _args(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["run_backtest", *argv])
    return rb._parse_args()


def _snapshot(monkeypatch, *argv, cfg: dict | None = None) -> dict:
    """The config_json a run launched with these flags would journal."""
    cfg = copy.deepcopy(cfg or BASE_CFG)
    pristine = copy.deepcopy(cfg)
    args = _args(monkeypatch, argv)
    port = rb._build_port_cfg(cfg, args, echo=False)
    default = rb._build_port_cfg(pristine, rb._neutral_args(args), echo=False)
    return rb._config_snapshot(cfg, port, default)


def test_overrides_with_a_yaml_home_are_recorded_at_their_effective_value(monkeypatch):
    snap = _snapshot(monkeypatch, "--breakeven-trigger-r", "1.5", "--max-hold-days", "30")
    assert snap["execution"]["breakeven_trigger_r"] == 1.5
    assert snap["execution"]["max_hold_days"] == 30


def test_overrides_with_no_yaml_home_are_recorded_under_portfolio(monkeypatch):
    """max_open_risk and trail_atr_mult exist only in base_port — there is no
    filters.yaml key to write them back to, so they need their own block."""
    snap = _snapshot(monkeypatch, "--max-open-risk", "10", "--trail-atr-mult", "3.0")
    assert snap["portfolio_overrides"] == {"max_open_risk": 10.0, "trail_atr_mult": 3.0}


def test_the_run_from_the_bug_report_shows_every_knob_it_moved(monkeypatch):
    """Run 38: five overrides, journalled as none of them."""
    snap = _snapshot(
        monkeypatch, "--max-open-risk", "10.0", "--breakeven-trigger-r", "1.5",
        "--max-hold-days", "30", "--max-hold-mode", "if-not-profit",
        "--trail-atr-mult", "3.0", "--chronic-penalty", "--anti-gap-entry",
    )
    moved = {d.split(":")[0] for d in config_mismatch(json.dumps(snap), _flatten(BASE_CFG))}
    assert moved == {"execution.breakeven_trigger_r", "execution.max_hold_days",
                     "portfolio_overrides.max_open_risk",
                     "portfolio_overrides.trail_atr_mult"}


def test_a_knob_switched_off_is_recorded_as_absent(monkeypatch):
    """--breakeven-trigger-r 0 disables the stop. Leaving the YAML's 1.0 in the
    snapshot would assert a breakeven the run never applied."""
    snap = _snapshot(monkeypatch, "--breakeven-trigger-r", "0")
    assert "breakeven_trigger_r" not in snap["execution"]
    assert any("breakeven_trigger_r" in d
               for d in config_mismatch(json.dumps(snap), _flatten(BASE_CFG)))


def test_forcing_a_yaml_disabled_switch_on_is_visible(monkeypatch):
    """--chronic-penalty ORs over the YAML. Against an enabled config that is a
    no-op; against a disabled one it changes the run and must show."""
    off = {**copy.deepcopy(BASE_CFG), "chronic_loser_penalty": {"enabled": False}}
    snap = _snapshot(monkeypatch, "--chronic-penalty", cfg=off)
    assert snap["portfolio_overrides"]["chronic_loser_cfg"]["enabled"] is True


def test_an_untouched_run_snapshots_exactly_as_it_did_before(monkeypatch):
    """Backward compatibility. A no-flag run must keep producing the snapshot the
    existing journal already holds — otherwise every historical row turns into a
    mismatch at once and reference_run loses its config-matched tier."""
    assert _snapshot(monkeypatch) == BASE_CFG
    assert config_mismatch(json.dumps(_snapshot(monkeypatch)), _flatten(BASE_CFG)) == []


def test_journalling_requires_the_portfolio_config(monkeypatch):
    """`port_cfg` must not be optional: an empty one is indistinguishable from
    "the run used none of the mapped knobs" and strips the execution block,
    marking a plain baseline as diverging from the shipped config on six keys."""
    import inspect

    sig = inspect.signature(rb._journal_baseline)
    for name in ("port_cfg", "default_port_cfg"):
        assert sig.parameters[name].default is inspect.Parameter.empty

    stripped = rb._config_snapshot(copy.deepcopy(BASE_CFG), {}, {})
    assert stripped["execution"] == {}          # what the old default produced


def test_a_knob_the_run_dropped_stays_visible_to_config_mismatch(monkeypatch):
    """A homeless knob absent from port_cfg is recorded False, not None: the
    shipped config has no portfolio_overrides at all, so a null would compare
    equal to "absent" and the difference would vanish."""
    snap = rb._config_snapshot(copy.deepcopy(BASE_CFG), {}, {"trail_atr_mult": 3.0})
    assert snap["portfolio_overrides"]["trail_atr_mult"] is False
    assert any("trail_atr_mult" in d
               for d in config_mismatch(json.dumps(snap), _flatten(BASE_CFG)))


def test_neutral_args_clears_every_flag_including_ones_added_later(monkeypatch):
    args = _args(monkeypatch, ["--max-open-risk", "10", "--allow-shorts"])
    neutral = rb._neutral_args(args)
    assert set(vars(neutral)) == set(vars(args))
    assert set(vars(neutral).values()) == {None}


def test_every_mapped_knob_the_builder_reads_has_a_yaml_home_entry(monkeypatch):
    """_PORT_YAML_HOME is an allowlist, and allowlists rot. A knob that has both
    a filters.yaml home and a CLI flag but no entry here would be written to
    `portfolio_overrides` while its YAML home kept the shipped value — one row
    asserting two values for one setting."""
    port = rb._build_port_cfg(copy.deepcopy(BASE_CFG),
                              rb._neutral_args(_args(monkeypatch, [])), echo=False)
    shared = set(port) & set(BASE_CFG["execution"])
    assert shared <= set(rb._PORT_YAML_HOME), f"unmapped: {shared - set(rb._PORT_YAML_HOME)}"


@pytest.mark.parametrize("argv,expect", [
    (["--max-open-risk", "10"], "portfolio_overrides.max_open_risk"),
    (["--breakeven-trigger-r", "1.5"], "execution.breakeven_trigger_r"),
])
def test_the_params_column_counts_the_override(monkeypatch, argv, expect):
    """End of the chain: GET /backtests must report the key, whether or not it
    has a filters.yaml entry to be diffed against."""
    from api.routers import backtests as bt

    snap = _snapshot(monkeypatch, *argv)
    diff, _window = bt._config_diff(json.dumps(snap), _flatten(BASE_CFG))
    assert expect in {d["key"] for d in diff}
