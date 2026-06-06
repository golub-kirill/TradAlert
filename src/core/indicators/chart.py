"""
4-panel dark-theme swing-trading chart, saved as WebP.

    Left sidebar      Scorecard (Trend Template criteria + values + macro)
    Panel 1  (4)      Candlesticks + MA50 + MA200
    Panel 2  (1)      Volume bars
    Panel 3  (1.5)    RSI(14) with overbought / oversold guides
    Panel 4  (2)      MACD histogram + MACD + signal

Signal annotation on the last bar fires when SignalResult.passed is True.
Supports long entry / long exit (and short / exit_short if added later).
"""

from __future__ import annotations

import io
import logging
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import yaml
from PIL import Image

from exceptions import ValidationError

logger = logging.getLogger(__name__)

# ----- constants -------------------------------------------------------------

LOOKBACK_BARS: int = 90
_WEBP_QUALITY: int = 90
_DPI: int = 150
_FIGSIZE = (20, 12)

DEFAULT_OUT_DIR = Path("data/screenshots")
_FILTERS_PATH = Path("config/filters.yaml")
_RSI_MID_DEFAULT = 50.0

# Layout (figure coordinates 0..1)
#  [SIDEBAR_LEFT ─── SIDEBAR_RIGHT]  GAP  [CHART_LEFT ─── CHART_RIGHT]
# Generous gaps prevent the chart's y-tick labels (right side) and the
# sidebar's content from being clipped at the figure edges.
_SIDEBAR_LEFT = 0.012
_SIDEBAR_RIGHT = 0.215
_CHART_LEFT = 0.255
_CHART_RIGHT = 0.940
_CHART_TOP = 0.925
_CHART_BOTTOM = 0.060


def _load_rsi_thresholds():
    if not _FILTERS_PATH.exists():
        logger.warning("filters.yaml not found at %s -- using defaults", _FILTERS_PATH)
        return 65.0, 35.0, _RSI_MID_DEFAULT
    cfg = yaml.safe_load(_FILTERS_PATH.read_text(encoding="utf-8")) or {}
    mr = cfg.get("signals", {}).get("mean_reversion", {})
    ob = float(mr.get("short", {}).get("rsi_min", 65))
    os_ = float(mr.get("long", {}).get("rsi_max", 35))
    return ob, os_, _RSI_MID_DEFAULT


_RSI_OB, _RSI_OS, _RSI_MID = _load_rsi_thresholds()

# ----- palette (TradingView-inspired dark) -----------------------------------
_C_BG = "#131722"
_C_PANEL = "#1a1d29"
_C_GRID = "#2a2e39"
_C_BORDER = "#363a45"
_C_TEXT = "#9598a1"
_C_TEXT_BRIGHT = "#d1d4dc"
_C_UP = "#26a69a"
_C_DOWN = "#ef5350"
_C_MA50 = "#FF9800"
_C_MA200 = "#F44336"
_C_RSI = "#ce93d8"
_C_MACD = "#42a5f5"
_C_SIGNAL = "#FF9800"
_C_ZERO = "#4a4e5e"
_C_AMBER = "#ffb74d"

_STYLE = mpf.make_mpf_style(
    base_mpl_style="dark_background",
    marketcolors=mpf.make_marketcolors(
        up=_C_UP, down=_C_DOWN, edge="inherit", wick="inherit",
        volume={"up": _C_UP + "66", "down": _C_DOWN + "66"},
    ),
    facecolor=_C_BG,
    edgecolor=_C_GRID,
    figcolor=_C_BG,
    gridcolor=_C_GRID,
    gridstyle="--",
    gridaxis="both",
    y_on_right=True,
    rc={
        "font.size": 9,
        "axes.labelcolor": _C_TEXT,
        "xtick.color": _C_TEXT,
        "ytick.color": _C_TEXT,
        "axes.titlecolor": _C_TEXT_BRIGHT,
    },
)


# ----- public API ------------------------------------------------------------

def chart(
        ticker,
        df,
        signal=None,
        output_dir=DEFAULT_OUT_DIR,
        lookback=LOOKBACK_BARS,
        historical_signals=None,
        regime=None,
        score_components=None,
        rp_rank=None,
):
    """Generate and save a 4-panel swing-trading chart as WebP."""
    _check_columns(df)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Filename embeds the SIGNAL BAR's date (the last bar in the chart), e.g.
    # URA_4jun26 (no leading-zero day, lowercase 3-letter month, 2-digit year),
    # so daily screenshots don't overwrite each other AND the name matches the bar
    # that fired. Falls back to today if df is empty. Built manually for
    # cross-platform support (Windows strftime lacks %-d).
    _d = df.index[-1] if len(df) else date.today()
    _stamp = f"{_d.day}{_d.strftime('%b').lower()}{_d.strftime('%y')}"
    out_path = output_dir / f"{ticker.upper()}_{_stamp}.webp"

    ma50 = df["close"].rolling(50, min_periods=1).mean()
    ma200 = df["close"].rolling(200, min_periods=1).mean()

    plot_df = df.tail(lookback).copy()
    ma50 = ma50.tail(lookback)
    ma200 = ma200.tail(lookback)

    mpf_df = plot_df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })[["Open", "High", "Low", "Close", "Volume"]]

    idx = plot_df.index
    rsi_ob = pd.Series(_RSI_OB, index=idx, dtype=float)
    rsi_os = pd.Series(_RSI_OS, index=idx, dtype=float)
    rsi_mid = pd.Series(_RSI_MID, index=idx, dtype=float)
    macd_zero = pd.Series(0.0, index=idx, dtype=float)

    hist = plot_df["macd_hist"]
    hist_colors = [_C_UP if v >= 0 else _C_DOWN for v in hist]

    addplots = [
        mpf.make_addplot(ma50, panel=0, color=_C_MA50, width=1.4, label="MA50"),
        mpf.make_addplot(ma200, panel=0, color=_C_MA200, width=1.4, label="MA200"),

        mpf.make_addplot(plot_df["rsi"], panel=2, color=_C_RSI, width=1.4, ylabel="RSI"),
        mpf.make_addplot(rsi_ob, panel=2, color=_C_DOWN, width=0.8,
                         linestyle="--", secondary_y=False),
        mpf.make_addplot(rsi_os, panel=2, color=_C_UP, width=0.8,
                         linestyle="--", secondary_y=False),
        mpf.make_addplot(rsi_mid, panel=2, color=_C_ZERO, width=0.6,
                         linestyle=":", secondary_y=False),

        mpf.make_addplot(hist, panel=3, type="bar",
                         color=hist_colors, ylabel="MACD", alpha=0.85),
        mpf.make_addplot(plot_df["macd"], panel=3, color=_C_MACD, width=1.2),
        mpf.make_addplot(plot_df["macd_signal"], panel=3, color=_C_SIGNAL, width=1.2),
        mpf.make_addplot(macd_zero, panel=3, color=_C_ZERO, width=0.6, linestyle=":"),
    ]

    bar_date = plot_df.index[-1].strftime("%Y-%m-%d")
    last_close_val = plot_df["close"].iloc[-1]
    last_rsi_val = plot_df["rsi"].iloc[-1]
    title = (
        "\n{tk}  -  Daily  -  {dt}    "
        "Close {cl:.2f}   RSI {r:.1f}   "
        "MA50 {m50:.2f}   MA200 {m200:.2f}"
    ).format(
        tk=ticker.upper(), dt=bar_date, cl=last_close_val, r=last_rsi_val,
        m50=ma50.iloc[-1], m200=ma200.iloc[-1],
    )

    fig, axes = mpf.plot(
        mpf_df,
        type="candle",
        style=_STYLE,
        addplot=addplots,
        volume=True,
        panel_ratios=(4, 1, 1.5, 2),
        figsize=_FIGSIZE,
        title=title,
        returnfig=True,
        tight_layout=False,
    )

    ax_price = axes[0]

    # Reposition all axes inside the [_CHART_LEFT, _CHART_RIGHT] x-range,
    # preserving mplfinance's vertical layout. Twin axes (odd indices) share
    # the same position rectangle.
    _reposition_axes(fig, axes)

    if score_components or regime is not None:
        _render_sidebar(
            fig, plot_df, df, ticker, signal=signal, regime=regime,
            score_components=score_components, rp_rank=rp_rank,
        )

    if signal and signal.passed:
        _annotate_signal(ax_price, plot_df, signal)

    if historical_signals:
        _annotate_historical_signals(ax_price, plot_df, df, historical_signals)

    _add_legend(ax_price)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, facecolor=_C_BG)
    buf.seek(0)
    plt.close(fig)

    Image.open(buf).save(str(out_path), "webp", quality=_WEBP_QUALITY, method=6)

    logger.info("Chart saved -> %s  (%s)", out_path, _file_size(out_path))
    return out_path.resolve()


# ----- private helpers -------------------------------------------------------

def _reposition_axes(fig, axes):
    """Snap every axis to the [_CHART_LEFT, _CHART_RIGHT] horizontal band.

    mplfinance returns paired axes (main + twin) — we map both to the same
    Bbox so the right-side y-tick labels stay inside the figure.
    """
    from matplotlib.transforms import Bbox

    for i in range(0, len(axes), 2):
        main_ax = axes[i]
        twin_ax = axes[i + 1] if i + 1 < len(axes) else None

        orig = main_ax.get_position()
        new_bbox = Bbox([[_CHART_LEFT, orig.y0], [_CHART_RIGHT, orig.y1]])
        main_ax.set_position(new_bbox)
        if twin_ax is not None:
            twin_ax.set_position(new_bbox)


def _check_columns(df):
    required = ["open", "high", "low", "close", "volume",
                "atr", "rsi", "macd", "macd_signal", "macd_hist"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValidationError("chart(): df missing columns: " + str(missing))


def _is_long_direction(d):
    return d in ("long", "buy", "entry_long")


def _is_short_direction(d):
    return d in ("short", "sell", "entry_short")


def _is_exit_direction(d):
    return d in ("exit_long", "exit_short", "exit", "close")


def _annotate_signal(ax, df, signal):
    """Annotate the last bar with a styled long / short / exit callout."""
    last_x = len(df) - 1
    last_close = float(df["close"].iloc[-1])
    last_high = float(df["high"].iloc[-1])
    last_low = float(df["low"].iloc[-1])
    bar_date = df.index[-1].strftime("%Y-%m-%d")
    direction = signal.direction or "none"

    is_long = _is_long_direction(direction)
    is_short = _is_short_direction(direction)
    is_exit = _is_exit_direction(direction)
    if not (is_long or is_short or is_exit):
        return

    score_val = getattr(signal, "score", 0.0)
    score_str = "  -  score {:.0f}/100".format(score_val) if score_val > 0 else ""

    # Expand y-limits up-front so SL / TP and the callout fit inside the panel.
    if not is_exit and signal.stop_price and signal.target_price:
        sl, tp = signal.stop_price, signal.target_price
        pad = max(abs(tp - sl) * 0.22, 1.0)
        cur_min, cur_max = ax.get_ylim()
        new_min = min(cur_min, min(sl, tp) - pad)
        new_max = max(cur_max, max(sl, tp) + pad)
        ax.set_ylim(new_min, new_max)

    y_min, y_max = ax.get_ylim()
    y_span = max(y_max - y_min, 1e-9)

    if is_long:
        color, marker, label_dir = _C_UP, "▲", "LONG"
        anchor_y = last_low
        text_y = max(anchor_y - y_span * 0.10, y_min + y_span * 0.16)
        va = "top"
    elif is_short:
        color, marker, label_dir = _C_DOWN, "▼", "SHORT"
        anchor_y = last_high
        text_y = min(anchor_y + y_span * 0.10, y_max - y_span * 0.18)
        va = "bottom"
    else:
        color, marker, label_dir = _C_AMBER, "✕", "EXIT"
        anchor_y = last_close
        text_y = min(anchor_y + y_span * 0.08, y_max - y_span * 0.10)
        va = "bottom"

    hold_str = ""
    if (is_long or is_short) and getattr(signal, "expected_hold_days", None):
        lo, hi = signal.expected_hold_days
        hold_str = "\nhold ~ {}-{}d".format(lo, hi)

    sig_type = getattr(signal, "signal_type", "") or ""
    type_str = "  " + sig_type if sig_type and sig_type != "none" else ""

    if is_exit:
        label = "{} {}{}{}\n{}  -  close {:.2f}".format(
            marker, label_dir, type_str, score_str, bar_date, last_close,
        )
    else:
        rr = ""
        if signal.stop_price and signal.target_price:
            risk = abs(last_close - signal.stop_price)
            reward = abs(signal.target_price - last_close)
            if risk > 0:
                rr = "  -  RR {:.1f}".format(reward / risk)
        label = (
            "{m} {d}{t}{s}\n"
            "{dt}  -  close {cl:.2f}{rr}\n"
            "SL {sl:.2f}   TP {tp:.2f}{h}"
        ).format(
            m=marker, d=label_dir, t=type_str, s=score_str,
            dt=bar_date, cl=last_close, rr=rr,
            sl=signal.stop_price, tp=signal.target_price, h=hold_str,
        )

    ax.annotate(
        label,
        xy=(last_x, anchor_y),
        xytext=(last_x - 6, text_y),
        fontsize=9,
        color=_C_TEXT_BRIGHT,
        fontweight="bold",
        bbox=dict(
            boxstyle="round,pad=0.5",
            facecolor=_C_PANEL,
            edgecolor=color,
            linewidth=1.5,
            alpha=0.92,
        ),
        arrowprops=dict(arrowstyle="->", color=color, lw=1.6, shrinkA=2, shrinkB=4),
        ha="right",
        va=va,
        zorder=20,
    )

    if is_exit:
        return

    if signal.stop_price:
        ax.axhline(signal.stop_price, color=color, linestyle="--",
                   linewidth=1.0, alpha=0.7, zorder=2)
        ax.text(
            0.005, signal.stop_price, " SL  {:.2f} ".format(signal.stop_price),
            transform=ax.get_yaxis_transform(),
            fontsize=8, color="#ffffff", fontweight="bold",
            bbox=dict(facecolor=color, edgecolor="none", pad=2, alpha=0.85),
            va="center", ha="left", zorder=3,
        )

    if signal.target_price:
        ax.axhline(signal.target_price, color=color, linestyle=":",
                   linewidth=1.0, alpha=0.7, zorder=2)
        ax.text(
            0.005, signal.target_price, " TP  {:.2f} ".format(signal.target_price),
            transform=ax.get_yaxis_transform(),
            fontsize=8, color="#ffffff", fontweight="bold",
            bbox=dict(facecolor=color, edgecolor="none", pad=2, alpha=0.7),
            va="center", ha="left", zorder=3,
        )
        ax.axhspan(
            min(last_close, signal.target_price),
            max(last_close, signal.target_price),
            alpha=0.07, color=color, zorder=1,
        )

    if signal.stop_price:
        ax.axhspan(
            min(last_close, signal.stop_price),
            max(last_close, signal.stop_price),
            alpha=0.05, color=_C_DOWN if is_long else _C_UP, zorder=1,
        )


def _add_legend(ax):
    from matplotlib.lines import Line2D
    handles = [
        Line2D([0], [0], color=_C_MA50, linewidth=1.6, label="MA 50"),
        Line2D([0], [0], color=_C_MA200, linewidth=1.6, label="MA 200"),
    ]
    ax.legend(
        handles=handles, loc="upper left", fontsize=9,
        facecolor=_C_PANEL, edgecolor=_C_BORDER, labelcolor=_C_TEXT_BRIGHT,
        framealpha=0.85,
    )


def _file_size(path):
    kb = path.stat().st_size / 1024
    return "{:.0f} KB".format(kb) if kb < 1024 else "{:.1f} MB".format(kb / 1024)


# ----- historical signal markers --------------------------------------------

def _annotate_historical_signals(ax, plot_df, full_df, historical_signals):
    plot_start = full_df.index[-len(plot_df)]
    plot_dates = {ts.date(): i for i, ts in enumerate(plot_df.index)}

    for hs in historical_signals:
        if not hs.passed or not hs.marker_symbol:
            continue
        if hs.bar_date < plot_start.date():
            continue

        bar_idx = plot_dates.get(hs.bar_date)
        if bar_idx is None:
            continue

        bar_high = float(plot_df["high"].iloc[bar_idx])
        bar_low = float(plot_df["low"].iloc[bar_idx])
        bar_close = float(plot_df["close"].iloc[bar_idx])

        is_long = hs.direction == "long"
        if is_long and not hs.watch_only:
            color = _C_UP
        elif hs.watch_only:
            color = _C_AMBER
        else:
            color = _C_DOWN

        if is_long:
            y = bar_low - bar_close * 0.012
            va = "top"
        else:
            y = bar_high + bar_close * 0.012
            va = "bottom"

        ax.text(
            bar_idx, y, hs.marker_symbol,
            fontsize=14, color=color, fontweight="bold",
            ha="center", va=va,
            alpha=0.65 if hs.watch_only else 0.95, zorder=10,
        )


# ----- sidebar scorecard -----------------------------------------------------

_CRITERIA_LABELS = {
    "rp_percentile": "RP > 70",
    "trend_up": "Price > SMA 50",
    "ma50_slope": "SMA 50 Rising",
    "ma200_slope": "SMA 200 Rising",
    "near_52w_high": "Within 25% of 52W High",
    "far_from_52w_low": "Price > 52W Low +30%",
    "macd_bullish": "MACD Bullish",
    "rsi_healthy": "RSI Healthy",
    "volume_spike": "Volume Spike",
    "breakout_20d": "20-day Breakout",
    "relative_strength": "Relative Strength",
    "no_earnings_risk": "No Earnings Risk",
    "weekly_trend": "Weekly Trend",
    "bb_zscore": "BB Z-score",
}


def _render_sidebar(fig, plot_df, full_df, ticker,
                    signal=None, regime=None,
                    score_components=None, rp_rank=None):
    """Render the left-side scorecard panel."""

    criteria_rows = []
    if score_components:
        for key, label in _CRITERIA_LABELS.items():
            if key in score_components:
                val = float(score_components[key])
                criteria_rows.append((label, val >= 0.5))

    passed_count = sum(1 for _, ok in criteria_rows if ok)
    total_count = len(criteria_rows)

    value_rows = []
    if rp_rank is not None:
        value_rows.append(("RP", str(rp_rank), _C_TEXT_BRIGHT))

    src = full_df if len(full_df) >= 252 else plot_df
    if len(src) >= 60:
        window = src.tail(252)
        close = float(window["close"].iloc[-1])
        high_52w = float(window["high"].max())
        low_52w = float(window["low"].min())
        if high_52w > 0:
            dist = (close - high_52w) / high_52w * 100
            if dist >= -10:
                color = _C_UP
            elif dist >= -25:
                color = _C_AMBER
            else:
                color = _C_DOWN
            value_rows.append(("Price vs 52W High", "{:+.1f}%".format(dist), color))
        if low_52w > 0:
            above = (close - low_52w) / low_52w * 100
            color = _C_UP if above >= 30 else _C_AMBER
            value_rows.append(("Price vs 52W Low", "+{:.1f}%".format(above), color))

    macro_rows = []
    macro_title = ""
    size_mult_str = ""
    if regime is not None:
        macro = getattr(regime, "macro", None)
        if macro is not None:
            risk_on = getattr(macro, "risk_on_score", None)
            if risk_on is not None:
                macro_title = "Macro   risk-on {:.2f}".format(risk_on)
            else:
                macro_title = "Macro"
            for attr, friendly in (
                    ("policy_stance_us", "Policy"),
                    ("curve_state", "Curve"),
                    ("credit_state", "Credit"),
            ):
                val = getattr(macro, attr, "")
                if val:
                    macro_rows.append((friendly, str(val)))
        if hasattr(regime, "size_multiplier"):
            try:
                size_mult_str = "{:.2f}x".format(float(regime.size_multiplier))
            except (TypeError, ValueError):
                size_mult_str = ""

    has_macro_section = bool(macro_title or macro_rows or size_mult_str)

    # Geometry — figure coordinates
    x0 = _SIDEBAR_LEFT
    x1 = _SIDEBAR_RIGHT
    pad_x = 0.014
    pad_y = 0.014
    row_h = 0.025
    header_h = 0.062
    section_gap = 0.014

    crit_h = (len(criteria_rows) * row_h + pad_y * 1.6) if criteria_rows else 0
    val_inner_rows = len(value_rows)
    val_h = (val_inner_rows * row_h + pad_y * 1.6 + row_h) if value_rows else 0
    macro_inner_rows = (1 if macro_title else 0) + len(macro_rows) + (1 if size_mult_str else 0)
    macro_h = (macro_inner_rows * row_h + pad_y * 1.6) if has_macro_section else 0

    n_sections = 1 + (1 if criteria_rows else 0) + (1 if value_rows else 0) + (1 if has_macro_section else 0)
    total_sidebar_h = header_h + crit_h + val_h + macro_h + (n_sections - 1) * section_gap

    y_center = (_CHART_TOP + _CHART_BOTTOM) / 2.0
    y_cursor = y_center + total_sidebar_h / 2.0

    # Header
    header_top = y_cursor
    header_bot = header_top - header_h
    _draw_panel(fig, x0, header_bot, x1 - x0, header_h)

    fig.text(
        x0 + pad_x, header_top - pad_y,
        ticker.upper(),
        fontsize=20, color=_C_TEXT_BRIGHT, fontweight="bold",
        ha="left", va="top", fontfamily="DejaVu Sans",
        zorder=101, transform=fig.transFigure,
    )

    badge_label, badge_color = _header_badge(signal, passed_count, total_count)
    if badge_label:
        fig.text(
            x1 - pad_x, header_top - pad_y - 0.004,
            badge_label,
            fontsize=12, color=badge_color, fontweight="bold",
            ha="right", va="top", fontfamily="DejaVu Sans",
            zorder=101, transform=fig.transFigure,
        )

    if total_count:
        fig.text(
            x0 + pad_x, header_top - pad_y - 0.032,
            "Trend Template   {} / {}".format(passed_count, total_count),
            fontsize=10, color=_C_TEXT, ha="left", va="top",
            fontfamily="DejaVu Sans",
            zorder=101, transform=fig.transFigure,
        )

    y_cursor = header_bot - section_gap

    # Criteria
    if criteria_rows:
        sect_top = y_cursor
        sect_bot = sect_top - crit_h
        _draw_panel(fig, x0, sect_bot, x1 - x0, crit_h)
        row_y = sect_top - pad_y
        for label, ok in criteria_rows:
            color = _C_UP if ok else _C_DOWN
            mark = "✓" if ok else "✗"
            fig.text(
                x0 + pad_x, row_y, label,
                fontsize=10.5, color=_C_TEXT_BRIGHT, ha="left", va="top",
                fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            fig.text(
                x1 - pad_x, row_y, mark,
                fontsize=14, color=color, fontweight="bold",
                ha="right", va="top",
                zorder=101, transform=fig.transFigure,
            )
            row_y -= row_h
        y_cursor = sect_bot - section_gap

    # Current values
    if value_rows:
        sect_top = y_cursor
        sect_bot = sect_top - val_h
        _draw_panel(fig, x0, sect_bot, x1 - x0, val_h)

        fig.text(
            x0 + pad_x, sect_top - pad_y,
            "Current Values",
            fontsize=11, color=_C_TEXT_BRIGHT, fontweight="bold",
            ha="left", va="top", fontfamily="DejaVu Sans",
            zorder=101, transform=fig.transFigure,
        )

        row_y = sect_top - pad_y - row_h
        for label, value, val_color in value_rows:
            fig.text(
                x0 + pad_x, row_y, label,
                fontsize=10, color=_C_TEXT, ha="left", va="top",
                fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            fig.text(
                x1 - pad_x, row_y, value,
                fontsize=10, color=val_color, fontweight="bold",
                ha="right", va="top", fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            row_y -= row_h
        y_cursor = sect_bot - section_gap

    # Macro
    if has_macro_section:
        sect_top = y_cursor
        sect_bot = sect_top - macro_h
        _draw_panel(fig, x0, sect_bot, x1 - x0, macro_h)
        row_y = sect_top - pad_y

        if macro_title:
            fig.text(
                x0 + pad_x, row_y, macro_title,
                fontsize=10.5, color=_C_TEXT_BRIGHT, fontweight="bold",
                ha="left", va="top", fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            row_y -= row_h

        for label, value in macro_rows:
            fig.text(
                x0 + pad_x, row_y, label,
                fontsize=10, color=_C_TEXT, ha="left", va="top",
                fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            fig.text(
                x1 - pad_x, row_y, value,
                fontsize=10, color=_C_TEXT_BRIGHT, ha="right", va="top",
                fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            row_y -= row_h

        if size_mult_str:
            fig.text(
                x0 + pad_x, row_y, "Size mult",
                fontsize=10, color=_C_TEXT, ha="left", va="top",
                fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )
            fig.text(
                x1 - pad_x, row_y, size_mult_str,
                fontsize=10, color=_C_AMBER, fontweight="bold",
                ha="right", va="top", fontfamily="DejaVu Sans",
                zorder=101, transform=fig.transFigure,
            )


def _header_badge(signal, passed_count, total_count):
    if signal and signal.passed:
        direction = signal.direction or ""
        score = getattr(signal, "score", 0.0)
        # Omit the score when it's 0 — i.e. scoring is OFF (signal un-enriched).
        # Showing "LONG  0" would read as a 0/100 confidence, which is wrong.
        sfx = "  {:.0f}".format(score) if score > 0 else ""
        if _is_long_direction(direction):
            return ("▲ LONG" + sfx, _C_UP)
        if _is_short_direction(direction):
            return ("▼ SHORT" + sfx, _C_DOWN)
        if _is_exit_direction(direction):
            return ("✕ EXIT" + sfx, _C_AMBER)
    if total_count:
        pct = passed_count / total_count
        if pct >= 0.75:
            return ("{}/{}".format(passed_count, total_count), _C_UP)
        if pct >= 0.5:
            return ("{}/{}".format(passed_count, total_count), _C_AMBER)
        return ("{}/{}".format(passed_count, total_count), _C_DOWN)
    return ("", _C_TEXT)


def _draw_panel(fig, x, y, width, height):
    from matplotlib.patches import FancyBboxPatch
    patch = FancyBboxPatch(
        (x, y), width, height,
        boxstyle="round,pad=0.003,rounding_size=0.006",
        facecolor=_C_PANEL, edgecolor=_C_BORDER, linewidth=1.0,
        alpha=0.97, transform=fig.transFigure, zorder=100,
    )
    fig.add_artist(patch)
