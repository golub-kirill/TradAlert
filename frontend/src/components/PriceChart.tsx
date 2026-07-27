/* Candlesticks + Bollinger + MA fast/slow + weekly SMA10, with synced Volume,
 * RSI and MACD panes and a live indicator legend.
 *
 * Rendered as real SVG elements. The previous version assembled four SVG
 * documents as strings and injected them with dangerouslySetInnerHTML, which
 * put server-provided date labels into markup; React escapes text nodes, so
 * that whole class of problem is gone along with the string building.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Bar } from "../api/types";
import { fnum } from "../lib/format";
import { hasFinePointer, rafThrottle } from "../lib/motion";

const VB = 600;
const CL = 6;
const CR = 560;

const fin = (v: number | null | undefined): v is number => v != null && !Number.isNaN(v);

const SERIES = {
  fast: "var(--series-2)",
  slow: "var(--series-4)",
  wsma: "var(--series-3)",
  up: "var(--series-1)",
  down: "var(--series-5)",
  band: "var(--text-muted)",
  grid: "var(--border)",
  label: "var(--text-muted)",
};

// ── shared primitives ───────────────────────────────────────────────────────
interface GridLine {
  y: number;
  label: string;
  dashed?: boolean;
}
interface Candle {
  x: number;
  yHigh: number;
  yLow: number;
  yTop: number;
  height: number;
  up: boolean;
}
interface Column {
  x: number;
  y: number;
  width: number;
  height: number;
  up: boolean;
}
interface Overlay {
  key: string;
  points: string;
  stroke: string;
  width: number;
  dash?: string;
}

function AxisLabel({ x, y, anchor, children }: { x: number; y: number; anchor?: "middle" | "start"; children: string }) {
  return (
    <text
      x={x}
      y={y}
      textAnchor={anchor}
      style={{ fill: SERIES.label, fontSize: 10, fontFamily: "var(--font-mono)" }}
    >
      {children}
    </text>
  );
}

function PaneTitle({ children }: { children: string }) {
  return (
    <text x={CL} y={11} style={{ fill: SERIES.label, fontSize: 10 }}>
      {children}
    </text>
  );
}

export function PriceChart({ bars }: { bars: Bar[] }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const m = useMemo(() => buildModel(bars), [bars]);
  const barCount = m?.n ?? 0;

  // Throttled to one update per frame: a hover step re-renders this component,
  // and the price pane alone is ~330 elements. Same treatment EquityCurve gets.
  // Declared before the empty-data return so the hook order never changes.
  const track = useMemo(
    () =>
      rafThrottle((clientX: number) => {
        const el = wrapRef.current;
        if (!el || barCount < 2) return;
        const r = el.getBoundingClientRect();
        const ratio = (clientX - r.left) / r.width;
        const i = Math.round((ratio * VB - CL) / ((CR - CL) / (barCount - 1)));
        setHover(Math.max(0, Math.min(barCount - 1, i)));
      }),
    [barCount],
  );
  useEffect(() => track.cancel, [track]);

  if (!m) {
    return (
      <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
        No data to chart.
      </p>
    );
  }
  const { n, dts, cs, lg, price, volume, rsi, macd } = m;

  const onMove = (e: React.MouseEvent) => {
    if (!hasFinePointer()) return;
    track(e.clientX);
  };

  const hoverPx = hover == null ? 0 : ((CL + hover * ((CR - CL) / (n - 1))) / VB) * 100;
  const c = hover == null ? null : cs[hover];
  const tipRight = hoverPx > 58;

  const swatch = (col: string) => <i className="legend__swatch" style={{ background: col }} />;

  return (
    <>
      <div className="legend">
        <span>
          {swatch(SERIES.fast)}MA50 {fnum(lg.maF, 2)}
        </span>
        <span>
          {swatch(SERIES.slow)}MA200 {fnum(lg.maS, 2)}
        </span>
        <span>
          {swatch(SERIES.wsma)}W-SMA10 {fnum(lg.wsma, 2)}
        </span>
        <span className="mut">
          BB {fnum(lg.bbU, 2)} / {fnum(lg.bbL, 2)}
        </span>
        <span>RSI {fnum(lg.rsi, 0)}</span>
        <span>ATR% {fnum(lg.atrPct, 2)}</span>
      </div>

      <div
        className="chartwrap"
        ref={wrapRef}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <svg
          viewBox={`0 0 ${VB} 210`}
          width="100%"
          role="img"
          aria-label={`Daily candlesticks with Bollinger bands and moving averages, ${dts[0]} to ${dts[n - 1]}.`}
        >
          {price.grid.map((g) => (
            <g key={`h${g.y}`}>
              <line x1={CL} x2={CR} y1={g.y} y2={g.y} stroke={SERIES.grid} strokeWidth={0.5} />
              <AxisLabel x={CR + 4} y={g.y + 3}>
                {g.label}
              </AxisLabel>
            </g>
          ))}
          {price.xTicks.map((t) => (
            <g key={`v${t.x}`}>
              <line
                x1={t.x}
                x2={t.x}
                y1={price.top}
                y2={price.bottom}
                stroke={SERIES.grid}
                strokeWidth={0.5}
                opacity={0.5}
              />
              <AxisLabel x={t.x} y={price.bottom + 13} anchor="middle">
                {t.label}
              </AxisLabel>
            </g>
          ))}

          {price.bbArea && <path d={price.bbArea} fill={SERIES.band} opacity={0.08} />}

          {price.candles.map((k, i) => (
            <g key={i} fill={k.up ? SERIES.up : SERIES.down} stroke={k.up ? SERIES.up : SERIES.down}>
              <line x1={k.x} x2={k.x} y1={k.yHigh} y2={k.yLow} strokeWidth={0.8} />
              <rect
                x={k.x - price.candleWidth / 2}
                y={k.yTop}
                width={price.candleWidth}
                height={k.height}
                stroke="none"
              />
            </g>
          ))}

          {price.overlays.map((o) => (
            <polyline
              key={o.key}
              points={o.points}
              fill="none"
              stroke={o.stroke}
              strokeWidth={o.width}
              strokeDasharray={o.dash}
            />
          ))}
        </svg>

        <div className={"crosshair" + (hover == null ? "" : " on")} style={{ left: hoverPx + "%" }} />
        {c && (
          <div
            className="tip on"
            style={{
              left: tipRight ? "auto" : hoverPx + "%",
              right: tipRight ? 100 - hoverPx + "%" : "auto",
            }}
          >
            <strong>{dts[hover!]}</strong> &nbsp;O {c[0].toFixed(2)} · H {c[1].toFixed(2)} · L{" "}
            {c[2].toFixed(2)} · C {c[3].toFixed(2)}
          </div>
        )}
      </div>

      <svg
        viewBox={`0 0 ${VB} 56`}
        width="100%"
        style={{ marginTop: "var(--sp-2)" }}
        role="img"
        aria-label="Traded volume per bar."
      >
        {volume.map((b, i) => (
          <rect
            key={i}
            x={b.x}
            y={b.y}
            width={b.width}
            height={b.height}
            fill={b.up ? SERIES.up : SERIES.down}
            opacity={0.45}
          />
        ))}
        <PaneTitle>Volume</PaneTitle>
      </svg>

      <svg
        viewBox={`0 0 ${VB} 66`}
        width="100%"
        style={{ marginTop: "var(--sp-2)" }}
        role="img"
        aria-label={`Relative strength index, currently ${fnum(lg.rsi, 0)}.`}
      >
        {rsi.grid.map((g) => (
          <g key={g.label}>
            <line
              x1={CL}
              x2={CR}
              y1={g.y}
              y2={g.y}
              stroke={SERIES.grid}
              strokeWidth={0.5}
              strokeDasharray={g.dashed ? "2 3" : undefined}
            />
            <text
              x={CR + 4}
              y={g.y + 3}
              style={{ fill: SERIES.label, fontSize: 9, fontFamily: "var(--font-mono)" }}
            >
              {g.label}
            </text>
          </g>
        ))}
        <polyline points={rsi.points} fill="none" stroke={SERIES.fast} strokeWidth={1.2} />
        <PaneTitle>RSI 14</PaneTitle>
      </svg>

      <svg
        viewBox={`0 0 ${VB} 68`}
        width="100%"
        style={{ marginTop: "var(--sp-2)" }}
        role="img"
        aria-label="MACD 12/26/9 with signal line and histogram."
      >
        <line x1={CL} x2={CR} y1={macd.zeroY} y2={macd.zeroY} stroke={SERIES.grid} strokeWidth={0.5} />
        {macd.hist.map((b, i) => (
          <rect
            key={i}
            x={b.x}
            y={b.y}
            width={b.width}
            height={b.height}
            fill={b.up ? SERIES.up : SERIES.down}
            opacity={0.55}
          />
        ))}
        <polyline points={macd.macdPoints} fill="none" stroke={SERIES.fast} strokeWidth={1.2} />
        <polyline points={macd.signalPoints} fill="none" stroke={SERIES.slow} strokeWidth={1.2} />
        <PaneTitle>MACD 12 26 9</PaneTitle>
      </svg>
    </>
  );
}

interface Legend {
  maF: number | null;
  maS: number | null;
  wsma: number | null;
  bbU: number | null;
  bbL: number | null;
  rsi: number | null;
  atrPct: number | null;
}

/** Pure geometry: numbers and point strings only, no markup. */
function buildModel(bars: Bar[]) {
  const n = bars.length;
  if (n < 2) return null;

  const cs = bars.map((b) => [b.open ?? 0, b.high ?? 0, b.low ?? 0, b.close ?? 0]);
  const dts = bars.map((b) => b.date.slice(5));
  const maf = bars.map((b) => b.ma_fast);
  const mas = bars.map((b) => b.ma_slow);
  const wsma = bars.map((b) => b.weekly_sma10);
  const bu = bars.map((b) => b.bb_upper);
  const bm = bars.map((b) => b.bb_mid);
  const bl = bars.map((b) => b.bb_lower);
  const vol = bars.map((b) => b.volume);
  const rsiV = bars.map((b) => b.rsi);
  const macdV = bars.map((b) => b.macd);
  const sigV = bars.map((b) => b.macd_signal);
  const histV = bars.map((b) => b.macd_hist);

  const top = 10;
  const bottom = 184;
  const all = cs.flat().concat(bu.filter(fin), bl.filter(fin), wsma.filter(fin));
  const mn = Math.min(...all);
  const mx = Math.max(...all);
  const pad = (mx - mn) * 0.05;
  const lo = mn - pad;
  const hi = mx + pad;

  const X = (i: number) => CL + i * ((CR - CL) / (n - 1));
  const candleWidth = ((CR - CL) / n) * 0.6;
  const Y = (v: number) => top + (1 - (v - lo) / (hi - lo || 1)) * (bottom - top);

  const pts = (a: (number | null)[], project: (v: number) => number) =>
    a
      .map((v, i) => (fin(v) ? `${X(i).toFixed(1)},${project(v).toFixed(1)}` : null))
      .filter(Boolean)
      .join(" ");

  const grid: GridLine[] = [];
  for (let k = 0; k <= 4; k++) {
    const y = top + k * ((bottom - top) / 4);
    grid.push({ y: +y.toFixed(1), label: (hi - (k / 4) * (hi - lo)).toFixed(0) });
  }

  // Deduped: for a very short series (n=2) the five evenly-spaced picks collapse
  // onto the same two bars, which would draw stacked ticks and, now that these
  // are real elements, collide on their React keys.
  const tickIdx = [...new Set(Array.from({ length: 5 }, (_, k) => Math.round((k * (n - 1)) / 4)))];
  const xTicks = tickIdx.map((i) => ({ x: +X(i).toFixed(1), label: dts[i] }));

  // Band fill across the bars that actually have both edges. The previous
  // version keyed off bu[0], which is null for the whole 20-period warmup of
  // every real series — so the fill silently never drew.
  const bandIdx = bu
    .map((v, i) => (fin(v) && fin(bl[i]) ? i : -1))
    .filter((i) => i >= 0);
  const bbArea =
    bandIdx.length > 1
      ? "M" +
        bandIdx.map((i) => `${X(i).toFixed(1)},${Y(bu[i] as number).toFixed(1)}`).join(" L") +
        " L" +
        [...bandIdx]
          .reverse()
          .map((i) => `${X(i).toFixed(1)},${Y(bl[i] as number).toFixed(1)}`)
          .join(" L") +
        "Z"
      : null;

  const candles: Candle[] = cs.map(([o, h, l, cc], i) => {
    const yTop = Y(Math.max(o, cc));
    const yBottom = Y(Math.min(o, cc));
    return {
      x: +X(i).toFixed(1),
      yHigh: +Y(h).toFixed(1),
      yLow: +Y(l).toFixed(1),
      yTop: +yTop.toFixed(1),
      height: +Math.max(0.8, yBottom - yTop).toFixed(1),
      up: cc >= o,
    };
  });

  const overlays: Overlay[] = [
    { key: "bbU", points: pts(bu, Y), stroke: SERIES.band, width: 0.6 },
    { key: "bbL", points: pts(bl, Y), stroke: SERIES.band, width: 0.6 },
    { key: "bbM", points: pts(bm, Y), stroke: SERIES.band, width: 0.7, dash: "2 3" },
    { key: "maS", points: pts(mas, Y), stroke: SERIES.slow, width: 1.3 },
    { key: "maF", points: pts(maf, Y), stroke: SERIES.fast, width: 1.3 },
    { key: "wsma", points: pts(wsma, Y), stroke: SERIES.wsma, width: 1.2 },
  ].filter((o) => o.points.length > 0);

  // ── volume ──
  const vTop = 8;
  const vBottom = 46;
  const vMax = Math.max(...vol.filter(fin), 1);
  const volume: Column[] = [];
  for (let i = 0; i < n; i++) {
    const v = vol[i];
    if (!fin(v)) continue;
    const y = vTop + (1 - v / vMax) * (vBottom - vTop);
    volume.push({
      x: +(X(i) - candleWidth / 2).toFixed(1),
      y: +y.toFixed(1),
      width: +candleWidth.toFixed(1),
      height: +Math.max(0.6, vBottom - y).toFixed(1),
      up: cs[i][3] >= cs[i][0],
    });
  }

  // ── RSI ──
  const rTop = 10;
  const rBottom = 54;
  const RY = (v: number) => rTop + (1 - v / 100) * (rBottom - rTop);
  const rsi = {
    grid: [70, 50, 30].map((v) => ({ y: +RY(v).toFixed(1), label: String(v), dashed: v !== 50 })),
    points: pts(rsiV, RY),
  };

  // ── MACD ──
  const mAbs = Math.max(
    ...macdV.filter(fin).map(Math.abs),
    ...sigV.filter(fin).map(Math.abs),
    0.01,
  );
  const mTop = 12;
  const mBottom = 56;
  const MY = (v: number) => mTop + (1 - (v / mAbs + 1) / 2) * (mBottom - mTop);
  const zeroY = +MY(0).toFixed(1);
  const hist: Column[] = [];
  for (let i = 0; i < n; i++) {
    const v = histV[i];
    if (!fin(v)) continue;
    const y = MY(v);
    hist.push({
      x: +(X(i) - candleWidth / 2).toFixed(1),
      y: +Math.min(zeroY, y).toFixed(1),
      width: +candleWidth.toFixed(1),
      height: +Math.max(0.6, Math.abs(zeroY - y)).toFixed(1),
      up: v >= 0,
    });
  }

  const last = bars[n - 1];
  const lg: Legend = {
    maF: last.ma_fast,
    maS: last.ma_slow,
    wsma: last.weekly_sma10,
    bbU: last.bb_upper,
    bbL: last.bb_lower,
    rsi: last.rsi,
    atrPct: last.atr != null && last.close ? (last.atr / last.close) * 100 : null,
  };

  return {
    n,
    dts,
    cs,
    lg,
    price: { top, bottom, grid, xTicks, bbArea, candles, overlays, candleWidth },
    volume,
    rsi,
    macd: { zeroY, hist, macdPoints: pts(macdV, MY), signalPoints: pts(sigV, MY) },
  };
}
