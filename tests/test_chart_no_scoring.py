"""Chart generation must work when scoring is OFF (signals un-enriched).

With `--scoring` off, main.py charts every fire without calling scorer.enrich, so
the signal reaches chart() with score=0, empty score_components, empty description.
These cover (1) the header badge omitting a 0 score and (2) a full headless render
not crashing on an un-enriched signal.
"""

from __future__ import annotations

from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")  # headless render for CI / no-display

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.indicators.chart import chart, _header_badge  # noqa: E402
from core.indicators.indicators import attach_indicators  # noqa: E402
from core.filter_engine import SignalResult  # noqa: E402


# ── header badge ─────────────────────────────────────────────────────────────

def test_header_badge_omits_zero_score():
    sig = SimpleNamespace(passed=True, direction="long", score=0.0)
    label, _color = _header_badge(sig, 0, 0)
    assert label == "▲ LONG"  # no misleading "0"


def test_header_badge_shows_positive_score():
    sig = SimpleNamespace(passed=True, direction="long", score=76.0)
    label, _color = _header_badge(sig, 5, 8)
    assert "LONG" in label and "76" in label


# ── full render, un-enriched signal ──────────────────────────────────────────

def _synthetic_df(n: int = 320) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    t = np.arange(n, dtype=float)
    close = 100.0 + 10.0 * np.sin(t / 15.0) + t * 0.05  # oscillating uptrend
    open_ = close - 0.2 * np.sin(t / 7.0)
    df = pd.DataFrame(
        {"open": open_, "high": close + 1.0, "low": close - 1.0,
         "close": close, "volume": 1_000_000 + 100_000 * np.abs(np.sin(t / 5.0))},
        index=idx,
    )
    return attach_indicators(df)


def test_chart_renders_with_unenriched_signal(tmp_path):
    df = _synthetic_df()
    last = float(df["close"].iloc[-1])
    # Un-enriched: score=0, score_components={}, description="" (the scoring-OFF state).
    sig = SignalResult(
        passed=True, direction="long", signal_type="momentum",
        stop_price=last * 0.95, target_price=last * 1.10,
        market_regime="BULL_NORMAL", ticker_trend="UPTREND", reason="test",
    )
    assert sig.score == 0.0 and not sig.score_components

    # regime present (main.py always passes it) so the sidebar path is exercised.
    regime = SimpleNamespace(macro=None, size_multiplier=1.0)
    out = chart("TEST", df, signal=sig, output_dir=tmp_path,
                score_components=None, regime=regime)

    assert out.exists() and out.stat().st_size > 0
