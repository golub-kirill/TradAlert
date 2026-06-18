"""Sweep-job settings hoist (backtest.sweep._base_settings / _job_settings).

config/settings.yaml is read ONCE per process and cached; each job deep-copies
the base and applies its own settings-resident mutation. Lock that: the base is
cached, baseline jobs get the base unmutated, non-baseline jobs get an ISOLATED
mutated copy, and a job's mutation never leaks into the cached base or the next
job (the property the old per-job disk re-read provided for free).
"""

from __future__ import annotations

from backtest.sweep import _base_settings, _job_settings

_KEY = "behavioral.size_mult_floor"   # a settings-resident sweep param


def test_base_settings_cached():
    a = _base_settings()
    b = _base_settings()
    assert a is b                      # one read per process, then reused
    assert isinstance(a, dict)
    assert "behavioral" in a           # the real settings.yaml content


def test_baseline_job_is_unmutated_deep_copy():
    base = _base_settings()
    s = _job_settings(_KEY, 0.9, is_baseline=True, mutations=None)
    assert s is not base               # a deep copy, never the cached base
    assert s == base                   # baseline → no mutation applied


def test_non_baseline_mutation_is_isolated():
    base = _base_settings()
    before = base.get("behavioral", {}).get("size_mult_floor")
    s = _job_settings(_KEY, 0.123, is_baseline=False, mutations=None)
    assert s["behavioral"]["size_mult_floor"] == 0.123          # applied to this job
    assert base.get("behavioral", {}).get("size_mult_floor") == before  # base untouched
    # a fresh job sees the original base, not the prior job's mutation
    s2 = _job_settings(_KEY, 0.5, is_baseline=False, mutations=None)
    assert s2["behavioral"]["size_mult_floor"] == 0.5


def test_mutations_dict_routes_every_entry():
    s = _job_settings("unused", "unused", is_baseline=False,
                      mutations={_KEY: 0.42})
    assert s["behavioral"]["size_mult_floor"] == 0.42
