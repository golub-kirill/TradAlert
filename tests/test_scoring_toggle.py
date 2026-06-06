"""`--scoring` toggle: the SignalScorer (min_score_to_alert gate + score-ranked
budget fill) is wired only when use_scoring is on. Default is OFF — the entry
score is non-predictive of realized R (corr -0.03), and its ranking selects
weaker trades under the open-risk budget.

Disk-free: an empty universe + the tracked config exercises the gating branch in
SweepEngine._run_one without needing the (gitignored) price cache.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from backtest import sweep
from backtest.loader import DateRange, UniverseData

_ROOT = Path(__file__).resolve().parent.parent


def _empty_universe() -> UniverseData:
    return UniverseData(
        prepped={}, market_dfs={}, vix_df=None, skipped={},
        date_range=DateRange(first=date(2020, 1, 1), last=date(2020, 1, 2)),
        tickers=[],
    )


def _base_cfg() -> dict:
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _count_scorer_inits(monkeypatch) -> list:
    # _run_one does a local `from core.scoring import SignalScorer`, so patch the
    # source module (resolved at call time).
    import core.scoring as scoring
    calls: list = []
    monkeypatch.setattr(scoring, "SignalScorer",
                        lambda *a, **k: (calls.append(1), object())[1])
    return calls


def test_default_is_scoring_off():
    eng = sweep.SweepEngine(_empty_universe(), _base_cfg(), n_workers=0)
    assert eng._use_scoring is False


def test_scorer_not_created_when_off(monkeypatch):
    calls = _count_scorer_inits(monkeypatch)
    eng = sweep.SweepEngine(_empty_universe(), _base_cfg(), n_workers=0,
                            use_scoring=False)
    eng.baseline()
    assert calls == []  # scorer never instantiated when scoring is OFF


def test_scorer_created_when_on(monkeypatch):
    calls = _count_scorer_inits(monkeypatch)
    eng = sweep.SweepEngine(_empty_universe(), _base_cfg(), n_workers=0,
                            use_scoring=True)
    eng.baseline()
    assert calls == [1]  # scorer instantiated exactly once when scoring is ON
