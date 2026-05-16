"""
Phase 5 verification: market-cap fetcher cache logic.
Run from project root: python3 tests/test_phase5.py

Network-free: every test exercises only the cache helpers, never _fetch().
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.fetchers import info_fetcher as info


# ── helpers ─────────────────────────────────────────────────────────────────

def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="tradealert_phase5_"))


def _write_cache(
    cache_dir: Path,
    ticker:    str,
    value:     float | None,
    age_h:     float = 0.0,
) -> Path:
    """Write a cache file with the given age in hours."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"{ticker.upper()}.json"
    p.write_text(json.dumps({
        "ticker":     ticker.upper(),
        "market_cap": value,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }))
    if age_h > 0:
        old = (datetime.now() - timedelta(hours=age_h)).timestamp()
        import os
        os.utime(p, (old, old))
    return p


# ── tests ───────────────────────────────────────────────────────────────────

def test_fresh_cache_hit_returns_value():
    d = _tmpdir()
    try:
        _write_cache(d, "AAPL", 2.9e12, age_h=1.0)
        result = info.get_market_cap("AAPL", cache_dir=d, staleness_hours=24)
        assert result == 2.9e12, f"got {result}"
        print(f"  PASS  fresh cache hit returns {result:.2e}")
    finally:
        shutil.rmtree(d)


def test_fresh_cache_hit_returns_none_when_cached_none():
    """Cached None (ETF/index) must survive within the staleness window."""
    d = _tmpdir()
    try:
        _write_cache(d, "SPY", None, age_h=1.0)
        result = info.get_market_cap("SPY", cache_dir=d, staleness_hours=24)
        assert result is None
        print(f"  PASS  cached None preserved on hit (ETF case)")
    finally:
        shutil.rmtree(d)


def test_stale_cache_triggers_refetch():
    """Stale file → cache miss → _fetch is called. We monkeypatch _fetch
    to avoid the network and confirm the wiring."""
    d = _tmpdir()
    try:
        _write_cache(d, "AAPL", 1.0e12, age_h=48.0)

        called = {"n": 0}
        def fake_fetch(ticker):
            called["n"] += 1
            return 9.99e12
        original_fetch = info._fetch
        info._fetch = fake_fetch
        try:
            result = info.get_market_cap("AAPL", cache_dir=d, staleness_hours=24)
        finally:
            info._fetch = original_fetch

        assert called["n"] == 1, f"_fetch called {called['n']} times, expected 1"
        assert result == 9.99e12, f"expected refetched value, got {result}"

        # New value must be persisted to cache
        p       = d / "AAPL.json"
        payload = json.loads(p.read_text())
        assert payload["market_cap"] == 9.99e12
        print(f"  PASS  stale cache → refetch → cache rewritten")
    finally:
        shutil.rmtree(d)


def test_corrupt_cache_triggers_refetch():
    d = _tmpdir()
    try:
        (d).mkdir(parents=True, exist_ok=True)
        (d / "AAPL.json").write_text("{not valid json")

        called = {"n": 0}
        def fake_fetch(ticker):
            called["n"] += 1
            return 5.0e11
        original_fetch = info._fetch
        info._fetch = fake_fetch
        try:
            result = info.get_market_cap("AAPL", cache_dir=d, staleness_hours=24)
        finally:
            info._fetch = original_fetch

        assert called["n"] == 1
        assert result == 5.0e11
        print(f"  PASS  corrupt cache treated as miss")
    finally:
        shutil.rmtree(d)


def test_missing_cache_triggers_fetch_and_writes():
    d = _tmpdir()
    try:
        called = {"n": 0}
        def fake_fetch(ticker):
            called["n"] += 1
            return 1.5e11
        original_fetch = info._fetch
        info._fetch = fake_fetch
        try:
            result = info.get_market_cap("NEW", cache_dir=d, staleness_hours=24)
        finally:
            info._fetch = original_fetch

        assert called["n"] == 1
        assert result == 1.5e11
        # File must now exist
        assert (d / "NEW.json").exists()
        print(f"  PASS  missing cache → fetch + write")
    finally:
        shutil.rmtree(d)


def test_force_bypasses_fresh_cache():
    d = _tmpdir()
    try:
        _write_cache(d, "AAPL", 1.0e12, age_h=1.0)  # fresh

        called = {"n": 0}
        def fake_fetch(ticker):
            called["n"] += 1
            return 2.5e12
        original_fetch = info._fetch
        info._fetch = fake_fetch
        try:
            result = info.get_market_cap("AAPL", cache_dir=d,
                                         staleness_hours=24, force=True)
        finally:
            info._fetch = original_fetch

        assert called["n"] == 1, "force=True should bypass cache and call _fetch"
        assert result == 2.5e12
        print(f"  PASS  force=True bypasses fresh cache")
    finally:
        shutil.rmtree(d)


def test_none_result_is_persisted():
    """When _fetch returns None (ETF lookup), the None must be cached so we
    don't keep retrying within the staleness window."""
    d = _tmpdir()
    try:
        def fake_fetch(ticker):
            return None
        original_fetch = info._fetch
        info._fetch = fake_fetch
        try:
            result = info.get_market_cap("SPY", cache_dir=d, staleness_hours=24)
        finally:
            info._fetch = original_fetch

        assert result is None
        payload = json.loads((d / "SPY.json").read_text())
        assert payload["market_cap"] is None
        print(f"  PASS  None result persisted in cache")
    finally:
        shutil.rmtree(d)


def test_invalid_ticker_raises_fetcherror():
    """Garbage ticker should raise FetchError from validate_ticker."""
    from exceptions import FetchError
    try:
        info.get_market_cap("with space")
        assert False, "expected FetchError"
    except FetchError as e:
        msg = str(e).lower()
        assert "invalid" in msg or "character" in msg
        print(f"  PASS  invalid ticker rejected by validator")


# ── runner ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_fresh_cache_hit_returns_value,
        test_fresh_cache_hit_returns_none_when_cached_none,
        test_stale_cache_triggers_refetch,
        test_corrupt_cache_triggers_refetch,
        test_missing_cache_triggers_fetch_and_writes,
        test_force_bypasses_fresh_cache,
        test_none_result_is_persisted,
        test_invalid_ticker_raises_fetcherror,
    ]
    failures = 0
    for t in tests:
        print(f"\n→ {t.__name__}")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {type(e).__name__}: {e}")
    print(f"\n{'─' * 60}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(failures)
