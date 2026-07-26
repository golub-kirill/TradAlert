/* Two panes over the same monthly series:
 *   • the cumulative-R equity curve (EquityCurve, self-drawing)
 *   • monthly net R as bars around zero
 * Rebuilt as real SVG elements — the previous version assembled an HTML string
 * and injected it with dangerouslySetInnerHTML. */

import { useMemo } from "react";
import type { MonthlyBar } from "../api/types";
import { EquityCurve, type CurvePoint } from "./EquityCurve";

const VB_W = 1000;
const VB_H = 96;

function tooFew(months: MonthlyBar[]) {
  return months.length < 2;
}

function NotEnough() {
  return (
    <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
      Not enough journaled trades to chart this run.
    </p>
  );
}

/** Monthly net R around zero. Split out so a view that already headlines the
 *  equity curve can show the distribution without drawing the curve twice. */
export function MonthlyBars({ months }: { months: MonthlyBar[] }) {
  const bars = useMemo(() => {
    if (!months.length) return null;
    const rMax = Math.max(...months.map((m) => Math.abs(m.r)), 0.01);
    const zero = VB_H / 2;
    const slot = VB_W / months.length;
    const w = Math.max(1.5, slot * 0.62);
    return months.map((m, i) => {
      const h = (Math.abs(m.r) / rMax) * (VB_H / 2 - 6);
      return {
        key: m.month,
        x: i * slot + (slot - w) / 2,
        y: m.r >= 0 ? zero - h : zero,
        w,
        h: Math.max(1, h),
        up: m.r >= 0,
        r: m.r,
      };
    });
  }, [months]);

  if (tooFew(months)) return <NotEnough />;

  const first = months[0].month;
  const last = months[months.length - 1].month;

  return (
    <div>
      <div className="legend">
        <span>
          <i className="legend__swatch" style={{ background: "var(--series-1)" }} />
          Month up
        </span>
        <span>
          <i className="legend__swatch" style={{ background: "var(--series-5)" }} />
          Month down
        </span>
      </div>
      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        width="100%"
        role="img"
        aria-label={`Monthly net R from ${first} to ${last}.`}
      >
        <line
          x1="0"
          x2={VB_W}
          y1={VB_H / 2}
          y2={VB_H / 2}
          stroke="var(--border-strong)"
          strokeWidth="1"
          vectorEffect="non-scaling-stroke"
        />
        {bars?.map((b) => (
          <rect
            key={b.key}
            x={b.x}
            y={b.y}
            width={b.w}
            height={b.h}
            rx="1"
            fill={b.up ? "var(--series-1)" : "var(--series-5)"}
            fillOpacity="0.85"
          >
            <title>{`${b.key} · ${b.r >= 0 ? "+" : ""}${b.r.toFixed(2)}R`}</title>
          </rect>
        ))}
      </svg>
      <div
        className="mut"
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--fs-micro)",
          marginTop: "var(--sp-2)",
        }}
      >
        <span>{first}</span>
        <span>{last}</span>
      </div>
    </div>
  );
}

/** Curve above, distribution below — for views that don't already headline the
 *  equity curve somewhere else on the page. */
export function PerformanceChart({ months }: { months: MonthlyBar[] }) {
  const curve: CurvePoint[] = useMemo(
    () => months.map((m) => ({ label: m.month, value: m.close })),
    [months],
  );

  if (tooFew(months)) return <NotEnough />;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--sp-5)" }}>
      <div>
        <div className="legend">
          <span>
            <i className="legend__swatch" style={{ background: "var(--series-1)" }} />
            Cumulative R
          </span>
        </div>
        <EquityCurve points={curve} height={200} interactive />
      </div>
      <MonthlyBars months={months} />
    </div>
  );
}
