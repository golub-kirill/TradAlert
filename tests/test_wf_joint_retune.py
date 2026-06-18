"""
Joint (multi-knob) walk-forward re-tune: the OFAT re-tune can only ship a
single-knob change per window, so its degradation understates the overfitting
of a multi-parameter selection. run_random_joint() samples seeded multi-knob
configs; the selected combo's FULL mutation dict must replay on the OOS leg.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from backtest.sweep import ParamSpec, SweepEngine
from backtest.walk_forward import WalkForwardEngine, WFWindow


GRID = [
    ParamSpec("a.x", (1, 2, 3), "AX", "g"),
    ParamSpec("a.y", (10, 20), "AY", "g"),
    ParamSpec("b.z", (0.1, 0.2, 0.3), "BZ", "g"),
]

BASE_CFG = {"a": {"x": 1, "y": 10}, "b": {"z": 0.1}}
BASE_PORT = {"max_open_risk": 5.0}


def _stub_point(param_name="p", param_value=0, is_baseline=False):
    return SimpleNamespace(
        param_name=param_name, param_value=param_value,
        is_baseline=is_baseline,
        stats=SimpleNamespace(trades_count=50, expectancy_r=0.05),
    )


@pytest.fixture
def engine(monkeypatch):
    calls = []

    def fake_run_one(self, cfg, port_params, param_name, param_value,
                     param_label, group, is_baseline=False, mutations=None):
        calls.append({
            "cfg": cfg, "port": dict(port_params),
            "param_name": param_name, "param_value": param_value,
            "is_baseline": is_baseline, "mutations": mutations,
        })
        return _stub_point(param_name, param_value, is_baseline)

    monkeypatch.setattr(SweepEngine, "_run_one", fake_run_one)
    eng = SweepEngine(
        universe=SimpleNamespace(summary=lambda: "stub"),
        base_cfg=BASE_CFG,
        base_port_cfg=dict(BASE_PORT),
        n_workers=0,
    )
    eng._calls = calls
    return eng


def test_joint_samples_mutate_k_knobs_with_non_baseline_values(engine):
    report = engine.run_random_joint(4, knobs=2, seed=42, grid=GRID, port_grid=[])
    assert len(report.points) == 4
    for pt in report.points:
        assert len(pt.mutations) == 2
        for dotted, val in pt.mutations.items():
            spec = next(s for s in GRID if s.dotted == dotted)
            assert val in spec.values
            # never the baseline value — that combo is the baseline's job
            baseline = {"a.x": 1, "a.y": 10, "b.z": 0.1}[dotted]
            assert val != baseline


def test_joint_sampler_is_deterministic_and_unique(engine, monkeypatch):
    r1 = engine.run_random_joint(6, knobs=2, seed=7, grid=GRID, port_grid=[])
    r2 = engine.run_random_joint(6, knobs=2, seed=7, grid=GRID, port_grid=[])
    muts1 = [p.mutations for p in r1.points]
    muts2 = [p.mutations for p in r2.points]
    assert muts1 == muts2
    keys = [frozenset(m.items()) for m in muts1]
    assert len(set(keys)) == len(keys)  # no duplicate combos


def test_joint_mutations_actually_reach_cfg_and_port(engine):
    grid = GRID + [ParamSpec("portfolio.max_open_risk", (5.0, 7.0), "Risk", "port")]
    engine.run_random_joint(8, knobs=4, seed=1, grid=grid, port_grid=[])
    joint_calls = [c for c in engine._calls if c["param_name"] == "joint"]
    assert joint_calls
    for call in joint_calls:
        cfg = call["cfg"]
        # the portfolio knob must land in port_params, never inside cfg
        assert "portfolio" not in cfg
        assert call["port"]["max_open_risk"] in (5.0, 7.0)
    # with knobs=4 > 3 engine specs + 1 port spec, every call mutates all four
    assert any(c["port"]["max_open_risk"] == 7.0 for c in joint_calls)


def test_exhausted_combo_space_stops_short(engine):
    # a.y has a single non-baseline value; knobs=3 → only 2*1*2=4 unique combos
    report = engine.run_random_joint(50, knobs=3, seed=3, grid=GRID, port_grid=[])
    assert len(report.points) == 4


def test_mutations_dict_is_forwarded_to_run_one(engine):
    """The settings channel (_apply_settings_mutation) is keyed per mutation
    inside _run_one — multi-knob jobs must forward the full dict, or
    settings-resident knobs (scanner.*, behavioral.*) become silent no-ops."""
    engine.run_random_joint(3, knobs=2, seed=11, grid=GRID, port_grid=[])
    joint_calls = [c for c in engine._calls if c["param_name"] == "joint"]
    assert joint_calls
    for call in joint_calls:
        assert isinstance(call["mutations"], dict)
        assert len(call["mutations"]) == 2


def test_resolve_baseline_falls_back_to_settings(engine, monkeypatch):
    """Settings-routed specs don't exist in filters.yaml; without the
    settings.yaml fallback their live baseline values enter the pool as fake
    mutations and pad the trial count."""
    monkeypatch.setattr(
        SweepEngine, "_load_settings",
        lambda self: {"behavioral": {"size_mult_floor": 0.25}},
    )
    spec = ParamSpec("behavioral.size_mult_floor", (0.25, 0.5, 0.65),
                     "Floor", "behavioral")
    assert engine._resolve_baseline(spec) == 0.25
    report = engine.run_random_joint(10, knobs=1, seed=5, grid=[spec],
                                     port_grid=[])
    sampled = {v for p in report.points for v in p.mutations.values()}
    assert 0.25 not in sampled
    assert sampled == {0.5, 0.65}


def _wf_engine(monkeypatch):
    calls = []

    def fake_run_one(self, cfg, port_params, param_name, param_value,
                     param_label, group, is_baseline=False, mutations=None):
        calls.append({"cfg": cfg, "port": dict(port_params),
                      "param_value": param_value, "mutations": mutations})
        return _stub_point(param_name, param_value, is_baseline)

    monkeypatch.setattr(SweepEngine, "_run_one", fake_run_one)
    wfe = WalkForwardEngine(
        universe=SimpleNamespace(summary=lambda: "stub"),
        base_cfg=BASE_CFG,
        base_port_cfg=dict(BASE_PORT),
        re_tune=True,
        grid=GRID,
        joint_samples=4,
        joint_knobs=2,
    )
    return wfe, calls


WIN = WFWindow(index=0, is_start=date(2018, 1, 1), is_end=date(2021, 1, 1),
               oos_start=date(2021, 1, 2), oos_end=date(2022, 1, 2))


def test_oos_leg_replays_the_full_mutation_dict(monkeypatch):
    wfe, calls = _wf_engine(monkeypatch)
    muts = {"a.x": 3, "b.z": 0.3, "portfolio.max_open_risk": 7.0}
    pt = wfe._run_window_with_mutations(WIN.oos_start, WIN.oos_end, WIN, muts)
    call = calls[-1]
    assert call["cfg"]["a"]["x"] == 3
    assert call["cfg"]["b"]["z"] == 0.3
    assert call["cfg"]["a"]["y"] == 10          # untouched knob stays baseline
    assert call["port"]["max_open_risk"] == 7.0
    assert call["port"]["start_date"] == WIN.oos_start
    assert call["port"]["end_date"] == WIN.oos_end
    assert call["mutations"] == muts            # settings channel gets them too
    assert pt.param_name == "wf_tuned"


def test_baseline_winner_replays_clean_base_config(monkeypatch):
    # When the IS winner is the baseline, the OOS leg must run the unmodified
    # base config (no injected top-level cfg["baseline"] key).
    wfe, calls = _wf_engine(monkeypatch)
    wfe._run_window_with_mutations(WIN.oos_start, WIN.oos_end, WIN, {})
    call = calls[-1]
    assert call["cfg"] == BASE_CFG
    assert "baseline" not in call["cfg"]
    assert call["param_value"] == "baseline"
