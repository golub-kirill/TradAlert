"""reconcile_live tier exclusion — fired NEEDS_REVIEW signals (stale/gapped data) must
be held OUT of the drift meter, and the held-out count surfaced (audit H4 / M6 context).

Verified without a DB: a fake dictionary-cursor records the SQL and feeds canned rows;
the backtest-side helpers (reference_run / trade_r_column) are stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "scripts" / "live"), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reconcile_live as rl  # noqa: E402


_SIG_SELECT = "FROM scan_results sr JOIN scan_runs"


class _FakeCursor:
    def __init__(self, sigs):
        self._sigs = sigs
        self._last = ""
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self._last = sql
        self.statements.append(sql)

    def fetchone(self):
        s = self._last
        if "information_schema.columns" in s:
            return {"n": 1}                       # tier column EXISTS
        if "tier = 'NEEDS_REVIEW'" in s:
            return {"n": 2}                       # two held-out fires
        if "AVG(" in s and "GROUP BY" not in s:
            return {"e": 0.42}                    # backtest E[R] overall
        return None

    def fetchall(self):
        if _SIG_SELECT in self._last:
            return self._sigs
        return []                                 # backtest_trades GROUP BY

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, dictionary=False):
        return self._cursor


def _sig(ticker="TEST.1"):
    return {"id": 1, "ticker": ticker, "signal_kind": "entry_long", "close": 100.0,
            "atr": 2.0, "stop_price": 95.0, "target_price": 110.0,
            "signal_type": "momentum", "created_at": "2026-01-05", "market_regime": "BULL_NORMAL"}


def _stub_backtest_db(monkeypatch):
    import backtest.db as bdb
    monkeypatch.setattr(bdb, "reference_run", lambda cur, rid: {
        "id": 42, "start_date": "2000-01-01", "end_date": None,
        "trades_count": 1622, "notes": None})
    monkeypatch.setattr(bdb, "trade_r_column", lambda cur: "effective_r")


def test_fetch_applies_tier_filter_and_counts_needs_review(monkeypatch):
    _stub_backtest_db(monkeypatch)
    cur = _FakeCursor([_sig()])
    sigs, exp, ref, needs_review = rl._fetch(_FakeConn(cur))

    sig_select = next(s for s in cur.statements if _SIG_SELECT in s)
    assert "sr.tier IS NULL OR sr.tier = 'LIVE'" in sig_select  # NEEDS_REVIEW excluded
    assert needs_review == 2                                     # exclusion counted, not silent
    assert sigs == [_sig()]
    assert ref["id"] == 42
    assert exp["__ALL__"][0] == 0.42
    # M6: backtest expectancy aggregated on per-unit r_multiple (like-for-like with
    # the live replay), NOT size-scaled effective_r.
    assert any("AVG(r_multiple)" in s for s in cur.statements)
    assert not any("AVG(effective_r)" in s for s in cur.statements)


def test_has_tier_column_probes_information_schema(monkeypatch):
    cur = _FakeCursor([])
    assert rl._has_tier_column(cur) is True
    assert any("information_schema.columns" in s for s in cur.statements)
