"""CLI behaviour for ``position_CLI`` — ``open --date`` parsing and pass-through.

These exercise the argument layer only; ``pm.open_position`` is stubbed so the
suite never touches MySQL.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import pytest

import position_CLI as cli


# ── _iso_date ───────────────────────────────────────────────────────────────

def test_iso_date_parses_valid():
    assert cli._iso_date("2026-05-28") == date(2026, 5, 28)


def test_iso_date_accepts_today():
    today = date.today()
    assert cli._iso_date(today.isoformat()) == today


def test_iso_date_rejects_bad_format():
    with pytest.raises(argparse.ArgumentTypeError):
        cli._iso_date("28/05/2026")


def test_iso_date_rejects_future():
    future = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(argparse.ArgumentTypeError):
        cli._iso_date(future)


# ── open --date pass-through ────────────────────────────────────────────────

def _capture_open(monkeypatch):
    captured: dict = {}

    def fake_open(**kwargs):
        captured.update(kwargs)
        return 42

    monkeypatch.setattr(cli.pm, "open_position", fake_open)
    return captured


def test_open_passes_explicit_date_through(monkeypatch):
    captured = _capture_open(monkeypatch)
    args = cli._build_parser().parse_args(
        ["open", "nvda", "138.10", "--date", "2026-05-28"])
    assert args.func(args) == 0
    assert captured["entry_date"] == date(2026, 5, 28)
    assert captured["ticker"] == "nvda"
    assert captured["entry_price"] == pytest.approx(138.10)


def test_open_defaults_to_today(monkeypatch):
    captured = _capture_open(monkeypatch)
    args = cli._build_parser().parse_args(["open", "aapl", "100"])
    assert args.func(args) == 0
    assert captured["entry_date"] == date.today()


def test_open_rejects_future_date_at_parse(monkeypatch):
    _capture_open(monkeypatch)
    future = (date.today() + timedelta(days=1)).isoformat()
    with pytest.raises(SystemExit):  # argparse exits on a bad argument value
        cli._build_parser().parse_args(["open", "aapl", "100", "--date", future])
