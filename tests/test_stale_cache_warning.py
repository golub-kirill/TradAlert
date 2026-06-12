"""
Stale-cache transparency (audit F2): when an upstream fetch fails and a macro
fetcher falls back to the cached parquet, it must WARN with the cache age if the
cache is past its staleness window. The data is still served (fail-open), but an
unbounded-stale cache can no longer masquerade silently as a fresh series.
"""

from __future__ import annotations

import os
import time

import pandas as pd

from core.fetchers import cache_meta
from core.fetchers.macro.boc import _load_cached_or_empty as boc_load
from core.fetchers.macro.fred import _load_cached_or_empty as fred_load
from core.fetchers.macro.yf_macro import _load_cached_or_empty as yf_load


def test_age_seconds(tmp_path):
    assert cache_meta.age_seconds(tmp_path / "missing.parquet") is None
    p = tmp_path / "x.parquet"
    pd.DataFrame({"value": [1.0]}).to_parquet(p)
    age = cache_meta.age_seconds(p)
    assert age is not None and age >= 0


def _aged_parquet(path, hours_old: float):
    pd.DataFrame({"value": [1.0, 2.0]}).to_parquet(path)
    old = time.time() - hours_old * 3600
    os.utime(path, (old, old))
    return path


def test_yf_macro_warns_when_serving_stale_cache(tmp_path, caplog):
    p = _aged_parquet(tmp_path / "CL=F.parquet", hours_old=100)  # window is 24h
    with caplog.at_level("WARNING"):
        df = yf_load(p, staleness_hours=24)
    assert len(df) == 2                        # still served (fail-open)
    assert "STALE cache" in caplog.text


def test_yf_macro_quiet_when_cache_fresh(tmp_path, caplog):
    p = _aged_parquet(tmp_path / "CL=F.parquet", hours_old=1)
    with caplog.at_level("WARNING"):
        df = yf_load(p, staleness_hours=24)
    assert len(df) == 2
    assert "STALE cache" not in caplog.text


def test_fred_warns_when_serving_stale_cache(tmp_path, caplog):
    p = _aged_parquet(tmp_path / "PCEPILFE.parquet", hours_old=100)
    with caplog.at_level("WARNING"):
        df = fred_load(p, staleness_hours=24)
    assert len(df) == 2
    assert "STALE cache" in caplog.text


def test_boc_warns_when_serving_stale_cache(tmp_path, caplog):
    p = _aged_parquet(tmp_path / "V39079.parquet", hours_old=100)
    with caplog.at_level("WARNING"):
        df = boc_load(p, staleness_hours=24)
    assert len(df) == 2
    assert "STALE cache" in caplog.text


def test_boc_quiet_when_cache_fresh(tmp_path, caplog):
    p = _aged_parquet(tmp_path / "V39079.parquet", hours_old=1)
    with caplog.at_level("WARNING"):
        df = boc_load(p, staleness_hours=24)
    assert len(df) == 2
    assert "STALE cache" not in caplog.text
