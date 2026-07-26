"""Crash-safe cache/state writes.

Every reader in this repo treats an unparseable cache as absent
(``except Exception: return {}``), so a truncated file is silently equivalent to
a lost one. Two places that costs something, both verified at file:line:

* ``intraday_monitor.load_state`` returns ``{}`` on any error, so a lost dedup
  state re-arms every alert — the operator gets duplicates.
* ``news_fetcher``'s AlphaVantage budget reader falls back to ``count = 0``, so a
  lost counter resets the daily tally and the free 25/day cap is exceeded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from persistence import atomic


def test_write_text_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deep" / "state.json"
    atomic.write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_json_round_trips(tmp_path):
    target = tmp_path / "cache.json"
    atomic.write_json(target, {"date": "2026-07-26", "count": 3}, indent=2)
    assert json.loads(target.read_text(encoding="utf-8")) == {"date": "2026-07-26", "count": 3}


def test_no_temp_file_is_left_behind(tmp_path):
    target = tmp_path / "cache.json"
    atomic.write_json(target, {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]


def test_previous_content_survives_a_failed_write(tmp_path, monkeypatch):
    """The whole point: a crash mid-write must leave the OLD file, not a
    truncated one. Path.write_text would have truncated before failing."""
    target = tmp_path / "state.json"
    atomic.write_json(target, {"armed": ["NVDA", "GS"]})

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", _boom)
    with pytest.raises(OSError):
        atomic.write_json(target, {"armed": []})

    assert json.loads(target.read_text(encoding="utf-8")) == {"armed": ["NVDA", "GS"]}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]   # tmp cleaned up


def test_unserialisable_object_does_not_touch_the_file(tmp_path):
    """json.dumps runs BEFORE the file is opened, so a bad payload cannot
    destroy a good cache."""
    target = tmp_path / "cache.json"
    atomic.write_json(target, {"good": 1})
    with pytest.raises(TypeError):
        atomic.write_json(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": 1}


def test_temp_file_is_a_sibling_of_the_target(tmp_path):
    """os.replace is only atomic within a volume, so the temp must not live in a
    system temp dir."""
    target = tmp_path / "sub" / "cache.json"
    assert atomic._tmp_for(target).parent == target.parent


def test_temp_name_is_process_unique(tmp_path):
    """Scanner, intraday monitor and daemon can be live at once and touch the
    same caches; a shared .tmp name would let one replace consume another's
    half-written file."""
    assert str(os.getpid()) in atomic._tmp_for(tmp_path / "cache.json").name


def test_keyboard_interrupt_also_cleans_up(tmp_path, monkeypatch):
    """A long scan is plausibly killed with Ctrl-C; that must not strand a
    sibling .tmp beside every cache it touched."""
    target = tmp_path / "cache.json"

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(atomic.os, "replace", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        atomic.write_json(target, {"a": 1})
    assert list(tmp_path.iterdir()) == []


# ── the two call sites whose loss actually costs something ────────────────────

def test_intraday_dedup_state_survives_a_failed_write(tmp_path, monkeypatch):
    import scripts.live.intraday_monitor as im  # noqa: PLC0415

    state_path = tmp_path / "intraday_state.json"
    monkeypatch.setattr(im, "_STATE_PATH", state_path)
    im.save_state({"NVDA": "2026-07-26"})
    assert im.load_state() == {"NVDA": "2026-07-26"}

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", _boom)
    im.save_state({})                      # fail-open: logs, does not raise
    # The armed state survived, so the next run does not re-alert NVDA.
    assert im.load_state() == {"NVDA": "2026-07-26"}
