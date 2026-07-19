"""Live chronic-loser penalty (ADOPTED 2026-07-17, D-011).

The scanner builds a journal-fed TickerHealth once per scan
(``main._load_ticker_health``) and scales fresh entries' ``size_mult`` by the
streak multiplier (``main._apply_chronic_penalty``), keeping the panel's
RISK/Size row in sync. Everything fails open: switch off, stub engines, or an
unreadable journal → no penalty, never a blocked scan.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import yaml

import main
from core.filter_engine import FilterEngine, GateCheck, SignalResult
from core.ticker_health import TickerHealth

_TODAY = date(2026, 7, 17)


def _pos(ticker="AAA", side="long", entry=100.0, stop=95.0, exit_px=90.0,
         exit_date=date(2026, 7, 1), initial_stop=None):
    return SimpleNamespace(
        ticker=ticker, side=side, entry_price=entry, entry_date=date(2026, 6, 1),
        stop_price=stop, initial_stop=initial_stop if initial_stop is not None else stop,
        exit_price=exit_px, exit_date=exit_date)


# ── _position_r (reconcile_fills convention, sign-level) ─────────────────────

def test_position_r_long_loss_and_win():
    assert main._position_r(_pos(exit_px=95.0)) == -1.0        # stopped: -1R
    assert main._position_r(_pos(exit_px=110.0)) == 2.0        # +2R winner


def test_position_r_short():
    p = _pos(side="short", entry=100.0, stop=105.0, exit_px=90.0)
    assert main._position_r(p) == 2.0
    p = _pos(side="short", entry=100.0, stop=105.0, exit_px=105.0)
    assert main._position_r(p) == -1.0


def test_position_r_falls_back_to_stop_price_and_rejects_degenerate():
    p = _pos(initial_stop=None, stop=95.0, exit_px=95.0)
    p.initial_stop = None
    assert main._position_r(p) == -1.0                          # legacy fallback
    assert main._position_r(_pos(stop=100.0, initial_stop=100.0)) is None  # risk 0
    assert main._position_r(_pos(exit_px=None)) is None         # still open


# ── _load_ticker_health (journal-fed, fail-open) ─────────────────────────────

def _engine(chronic_enabled: bool = True) -> FilterEngine:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["chronic_loser_penalty"] = {
        "enabled": chronic_enabled, "lookback_days": 90,
        "scale": {2: 0.5, 3: 0.25},
    }
    return FilterEngine.from_dict(cfg, today=_TODAY)


def test_health_builds_from_closed_journal_rows(monkeypatch):
    rows = [_pos(exit_px=95.0, exit_date=date(2026, 6, 20)),
            _pos(exit_px=95.0, exit_date=date(2026, 7, 1)),
            _pos(ticker="BBB", exit_px=110.0, exit_date=date(2026, 7, 1)),
            SimpleNamespace(ticker="OPEN", side="long", entry_price=1.0,
                            entry_date=_TODAY, stop_price=0.9, initial_stop=0.9,
                            exit_price=None, exit_date=None)]
    monkeypatch.setattr("core.position_manager.list_all", lambda: rows)
    health = main._load_ticker_health(_engine())
    assert health is not None
    assert health.size_multiplier("AAA", _TODAY) == 0.5   # 2-loss streak
    assert health.size_multiplier("BBB", _TODAY) == 1.0   # winner — no penalty


def test_health_none_when_switch_off(monkeypatch):
    monkeypatch.setattr("core.position_manager.list_all", lambda: [])
    assert main._load_ticker_health(_engine(chronic_enabled=False)) is None


def test_health_fails_open_on_journal_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("core.position_manager.list_all", _boom)
    assert main._load_ticker_health(_engine()) is None


def test_health_none_for_stub_engines_without_cfg():
    assert main._load_ticker_health(SimpleNamespace()) is None


# ── _apply_chronic_penalty (size + panel row in sync) ────────────────────────

def _fired(size_mult=1.0):
    return SignalResult(
        passed=True, direction="long", signal_type="momentum",
        stop_price=95.0, target_price=112.5, min_rr=2.5, size_mult=size_mult,
        market_regime="BULL_LOW", ticker_trend="UPTREND", reason="entry",
        checks=[GateCheck(group="RISK", name="Size", passed=True,
                          detail=f"{size_mult:.2f}x", strength=size_mult)])


def _health(losses: int) -> TickerHealth:
    h = TickerHealth(lookback_days=90, scale={2: 0.5, 3: 0.25})
    for i in range(losses):
        h.record_trade("AAA", date(2026, 6, 1 + i), -1.0)
    return h


def test_penalty_halves_size_and_patches_the_panel_row():
    sig = _fired()
    main._apply_chronic_penalty(sig, "AAA", _health(2), _TODAY)
    assert sig.size_mult == 0.5
    row = sig.checks[0]
    assert row.passed is True and "chronic 2L" in row.detail and row.strength == 0.5


def test_penalty_floor_marks_the_row_failed():
    sig = _fired()
    main._apply_chronic_penalty(sig, "AAA", _health(3), _TODAY)
    assert sig.size_mult == 0.25
    assert sig.checks[0].passed is False       # below the 0.5 display threshold


def test_no_streak_leaves_the_signal_untouched():
    sig = _fired()
    main._apply_chronic_penalty(sig, "AAA", _health(1), _TODAY)
    assert sig.size_mult == 1.0 and sig.checks[0].detail == "1.00x"
