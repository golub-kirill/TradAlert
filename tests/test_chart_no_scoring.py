"""Chart generation for plain engine signals.

Signals reach chart() exactly as the engine emits them — no enrichment layer.
These cover (1) the header badge rendering the direction alone and (2) a full
headless render not crashing on a plain SignalResult.
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

def test_header_badge_shows_direction_only():
    sig = SimpleNamespace(passed=True, direction="long")
    label, _color = _header_badge(sig, 0, 0)
    assert label == "▲ LONG"


# ── full render, plain engine signal ─────────────────────────────────────────

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


def test_chart_renders_with_plain_signal(tmp_path):
    df = _synthetic_df()
    last = float(df["close"].iloc[-1])
    sig = SignalResult(
        passed=True, direction="long", signal_type="momentum",
        stop_price=last * 0.95, target_price=last * 1.10,
        market_regime="BULL_NORMAL", ticker_trend="UPTREND", reason="test",
    )

    # regime present (main.py always passes it) so the sidebar path is exercised.
    regime = SimpleNamespace(macro=None, size_multiplier=1.0)
    out = chart("TEST", df, signal=sig, output_dir=tmp_path,
                score_components=None, regime=regime)

    assert out.exists() and out.stat().st_size > 0
