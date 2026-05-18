"""
4-panel dark-theme swing-trading chart, saved as WebP.

    Panel 1  Candlesticks + MA50 + MA200          (ratio 4)
    Panel 2  Volume bars                           (ratio 1)
    Panel 3  RSI(14) with overbought / oversold   (ratio 1.5)
    Panel 4  MACD histogram + MACD + signal       (ratio 2)

Signal annotation on the last bar fires when SignalResult.passed is True.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import yaml
from PIL import Image

from core.filter_engine import SignalResult
from exceptions import ValidationError

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

LOOKBACK_BARS: int = 90  # trading days shown — ~4 months
_WEBP_QUALITY: int = 90  # 0–100; 90 = near-lossless
_DPI:           int = 150       # 16×10 inch canvas @ 150 DPI → 2400×1500 px
_FIGSIZE            = (16, 10)

DEFAULT_OUT_DIR = Path("data/screenshots")
_FILTERS_PATH = Path("config/filters.yaml")
_RSI_MID_DEFAULT = 50.0


def _load_rsi_thresholds() -> tuple[float, float, float]:
    """
    Return (overbought, oversold, midline) RSI levels for chart guide lines.

    Reads ``signals.mean_reversion.short.rsi_min`` (overbought) and
    ``signals.mean_reversion.long.rsi_max`` (oversold) from filters.yaml.
    Midline is fixed at 50.0 (RSI scale midpoint).
    """
    if not _FILTERS_PATH.exists():
        logger.warning("filters.yaml not found at %s — using defaults 65/35/50", _FILTERS_PATH)
        return 65.0, 35.0, _RSI_MID_DEFAULT

    cfg = yaml.safe_load(_FILTERS_PATH.read_text()) or {}
    mr = cfg.get("signals", {}).get("mean_reversion", {})
    ob = float(mr.get("short", {}).get("rsi_min", 65))
    os_ = float(mr.get("long", {}).get("rsi_max", 35))
    return ob, os_, _RSI_MID_DEFAULT


_RSI_OB, _RSI_OS, _RSI_MID = _load_rsi_thresholds()

# ── Palette ─────────────────────────────────────────
_C_BG       = "#131722"
_C_GRID     = "#2a2e39"
_C_TEXT     = "#9598a1"
_C_UP       = "#26a69a"   # teal green
_C_DOWN     = "#ef5350"   # red
_C_MA50     = "#FF9800"   # orange
_C_MA200    = "#F44336"   # deep red
_C_RSI      = "#ce93d8"   # light purple
_C_MACD     = "#42a5f5"   # blue
_C_SIGNAL   = "#FF9800"   # orange (same as MA50 — signal line)
_C_ZERO     = "#4a4e5e"   # muted grey for zero / mid-lines

_STYLE = mpf.make_mpf_style(
    base_mpl_style = "dark_background",
    marketcolors   = mpf.make_marketcolors(
        up     = _C_UP,
        down   = _C_DOWN,
        edge   = "inherit",
        wick   = "inherit",
        volume = {"up": _C_UP + "66", "down": _C_DOWN + "66"},  # 40% alpha
    ),
    facecolor  = _C_BG,
    edgecolor  = _C_GRID,
    figcolor   = _C_BG,
    gridcolor  = _C_GRID,
    gridstyle  = "--",
    gridaxis   = "both",
    y_on_right = True,
    rc         = {
        "font.size":        9,
        "axes.labelcolor":  _C_TEXT,
        "xtick.color":      _C_TEXT,
        "ytick.color":      _C_TEXT,
        "axes.titlecolor":  "#d1d4dc",
    },
)


# ── public API ────────────────────────────────────────────────────────────────

def chart(
    ticker:     str,
    df:         pd.DataFrame,
    signal:     SignalResult | None = None,
    output_dir: Path | str          = DEFAULT_OUT_DIR,
    lookback:   int                 = LOOKBACK_BARS,
) -> Path:
    """
    Generate and save a 4-panel swing trading chart as WebP.

    Parameters
    ----------
    ticker     : Symbol — used in chart title and output filename.
    df         : Enriched OHLCV DataFrame.
                 Required columns: open high low close volume
                                   atr rsi macd macd_signal macd_hist.
                 DatetimeIndex (tz-naive). Length ≥ max(50, lookback) so MA50
                 has values across the displayed window.
    signal     : Optional SignalResult; ``passed=True`` annotates the last bar.
    output_dir : Output directory; created if missing.
    lookback   : Number of trading bars to display. Default 90.

    Returns
    -------
    Path  Absolute path to the saved .webp file.

    Raises
    ------
    ValidationError
        When required indicator columns are missing from df.
    """
    _check_columns(df)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{ticker.upper()}.webp"

    # ── 1. Compute MAs on full df → slice to display window ───────────────────
    # Compute on the full series so the displayed window has accurate values
    # even at the left edge (no warmup NaN in the visible bars).
    ma50  = df["close"].rolling(50,  min_periods=1).mean()
    ma200 = df["close"].rolling(200, min_periods=1).mean()

    # ── 2. Slice to lookback window ───────────────────────────────────────────
    plot_df = df.tail(lookback).copy()
    ma50    = ma50.tail(lookback)
    ma200   = ma200.tail(lookback)

    # ── 3. Build mplfinance-compatible DataFrame (capitalised columns) ─────────
    mpf_df = plot_df.rename(columns={
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
    })[["Open", "High", "Low", "Close", "Volume"]]

    # ── 4. Reference series ───────────────────────────────────────────────────
    idx      = plot_df.index
    rsi_ob   = pd.Series(_RSI_OB,  index=idx, dtype=float)
    rsi_os   = pd.Series(_RSI_OS,  index=idx, dtype=float)
    rsi_mid  = pd.Series(_RSI_MID, index=idx, dtype=float)
    macd_zero= pd.Series(0.0,      index=idx, dtype=float)

    # ── 5. MACD histogram colouring ───────────────────────────────────────────
    hist        = plot_df["macd_hist"]
    hist_colors = [_C_UP if v >= 0 else _C_DOWN for v in hist]

    # ── 6. Build addplots ─────────────────────────────────────────────────────
    # panel 0 = price candles  (built-in)
    # panel 1 = volume bars    (built-in via volume=True)
    # panel 2 = RSI            (addplot)
    # panel 3 = MACD           (addplot)

    addplots = [
        # — price panel: moving averages —
        mpf.make_addplot(ma50,  panel=0, color=_C_MA50,  width=1.2, label="MA50"),
        mpf.make_addplot(ma200, panel=0, color=_C_MA200, width=1.2, label="MA200"),

        # — RSI panel —
        mpf.make_addplot(plot_df["rsi"], panel=2, color=_C_RSI,  width=1.3,
                         ylabel="RSI"),
        mpf.make_addplot(rsi_ob,  panel=2, color=_C_DOWN,  width=0.8,
                         linestyle="--", secondary_y=False),
        mpf.make_addplot(rsi_os,  panel=2, color=_C_UP,    width=0.8,
                         linestyle="--", secondary_y=False),
        mpf.make_addplot(rsi_mid, panel=2, color=_C_ZERO,  width=0.6,
                         linestyle=":",  secondary_y=False),

        # — MACD panel —
        mpf.make_addplot(hist,              panel=3, type="bar",
                         color=hist_colors, ylabel="MACD"),
        mpf.make_addplot(plot_df["macd"],        panel=3, color=_C_MACD,
                         width=1.0),
        mpf.make_addplot(plot_df["macd_signal"], panel=3, color=_C_SIGNAL,
                         width=1.0),
        mpf.make_addplot(macd_zero, panel=3, color=_C_ZERO, width=0.6,
                         linestyle=":"),
    ]

    # ── 7. Render ─────────────────────────────────────────────────────────────
    bar_date = plot_df.index[-1].strftime("%Y-%m-%d")
    title = (
        f"\n{ticker.upper()}  —  Daily  |  RSI {plot_df['rsi'].iloc[-1]:.1f}  "
        f"|  MA50 {ma50.iloc[-1]:.2f}  |  MA200 {ma200.iloc[-1]:.2f}"
        f"  |  {bar_date}  close={plot_df['close'].iloc[-1]:.2f}"
    )

    fig, axes = mpf.plot(
        mpf_df,
        type         = "candle",
        style        = _STYLE,
        addplot      = addplots,
        volume       = True,
        panel_ratios = (4, 1, 1.5, 2),
        figsize      = _FIGSIZE,
        title        = title,
        returnfig    = True,
    )

    # axes[0] = price, axes[2] = volume, axes[4] = RSI, axes[6] = MACD
    # (mplfinance doubles axes for twin-axis support — odd indices are twins)
    ax_price = axes[0]

    # ── 8. Signal annotation ──────────────────────────────────────────────────
    if signal and signal.passed:
        _annotate_signal(ax_price, plot_df, signal)

    # ── 9. Legend on price panel ──────────────────────────────────────────────
    _add_legend(ax_price)

    # ── 10. Save PNG → convert to WebP ───────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(
        buf,
        format      = "png",
        dpi         = _DPI,
        bbox_inches = "tight",
        facecolor   = _C_BG,
    )
    buf.seek(0)
    plt.close(fig)

    Image.open(buf).save(str(out_path), "webp", quality=_WEBP_QUALITY, method=6)

    logger.info("Chart saved → %s  (%s)", out_path, _file_size(out_path))
    return out_path.resolve()


# ── private helpers ───────────────────────────────────────────────────────────

def _check_columns(df: pd.DataFrame) -> None:
    """Raise ValidationError when any required column is absent from df."""
    required = ["open", "high", "low", "close", "volume",
                "atr", "rsi", "macd", "macd_signal", "macd_hist"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValidationError(f"chart(): df missing columns: {missing}")


def _annotate_signal(
    ax:     plt.Axes,
    df:     pd.DataFrame,
    signal: SignalResult,
) -> None:
    """
    Annotate the last bar based on signal direction.

    Long entry  : ▲ LONG label + stop line + target zone.
    Long exit   : ✕ EXIT label only.
    """
    last_x     = len(df) - 1
    last_close = df["close"].iloc[-1]
    bar_date   = df.index[-1].strftime("%Y-%m-%d")
    is_entry   = signal.direction == "long"
    is_exit    = signal.direction == "exit_long"

    score_str = f"  score {signal.score:.0f}/100" if signal.score > 0 else ""

    if is_entry:
        color     = _C_UP
        arrow_dir = 1
        hold_str  = ""
        if hasattr(signal, "expected_hold_days") and signal.expected_hold_days:
            lo, hi = signal.expected_hold_days
            hold_str = f"\n~{lo}–{hi}d hold"
        label = (
            f"▲ LONG  {signal.signal_type}{score_str}"
            f"\n{bar_date}  close={last_close:.2f}"
            f"\nstop  {signal.stop_price:.2f}{hold_str}"
        )
        va = "bottom"
    elif is_exit:
        color     = _C_DOWN
        arrow_dir = -1
        label = (
            f"✕ EXIT  {signal.signal_type}{score_str}"
            f"\n{bar_date}  close={last_close:.2f}"
        )
        va = "top"
    else:
        return

    ax.annotate(
        label,
        xy       = (last_x, last_close),
        xytext   = (last_x - 4, last_close + arrow_dir * last_close * 0.025),
        fontsize = 8,
        color    = color,
        fontweight = "bold",
        arrowprops = dict(arrowstyle="->", color=color, lw=1.5),
        ha       = "right",
        va       = va,
    )

    if not is_entry:
        return  # exits don't draw stop/target lines

    # Stop line + label
    ax.axhline(signal.stop_price, color=color, linestyle="--",
               linewidth=0.9, alpha=0.8)
    ax.text(
        0.01, signal.stop_price,
        f"  SL  {signal.stop_price:.2f}",
        transform  = ax.get_yaxis_transform(),
        fontsize   = 7.5,
        color      = color,
        alpha      = 0.85,
        va         = "bottom",
    )

    # Target line + fill zone
    ax.axhline(signal.target_price, color=color, linestyle=":",
               linewidth=0.9, alpha=0.5)
    ax.text(
        0.01, signal.target_price,
        f"  TP  {signal.target_price:.2f}",
        transform  = ax.get_yaxis_transform(),
        fontsize   = 7.5,
        color      = color,
        alpha      = 0.75,
        va         = "bottom",
    )
    ax.axhspan(
        min(last_close, signal.target_price),
        max(last_close, signal.target_price),
        alpha=0.06, color=color,
    )


def _add_legend(ax: plt.Axes) -> None:
    """Add MA50 / MA200 legend to the price panel."""
    from matplotlib.lines import Line2D
    handles = [
        Line2D([0], [0], color=_C_MA50,  linewidth=1.2, label="MA 50"),
        Line2D([0], [0], color=_C_MA200, linewidth=1.2, label="MA 200"),
    ]
    ax.legend(
        handles  = handles,
        loc      = "upper left",
        fontsize = 8,
        facecolor= _C_BG,
        edgecolor= _C_GRID,
        labelcolor= _C_TEXT,
    )


def _file_size(path: Path) -> str:
    kb = path.stat().st_size / 1024
    return f"{kb:.0f} KB" if kb < 1024 else f"{kb/1024:.1f} MB"
