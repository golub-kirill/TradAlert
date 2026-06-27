import type { MonthlyBar } from "../api/types";

// Monthly equity candles: open/close/high/low of cumulative R per month.
// Green = winning month (close >= open), red = losing month. Numeric/server data
// only is interpolated into the SVG (safe to inject).
export function MonthlyCandles({ months }: { months: MonthlyBar[] }) {
  if (months.length < 1) return <p className="note">No trades to chart for the latest run.</p>;
  const VB_W = 720,
    VB_H = 214,
    L = 8,
    R = 684,
    T = 12,
    B = 184;
  const n = months.length;
  const vals = months.flatMap((m) => [m.high, m.low]).concat([0]);
  const mn = Math.min(...vals),
    mx = Math.max(...vals);
  const pad = (mx - mn) * 0.06 || 1;
  const lo = mn - pad,
    hi = mx + pad;
  const X = (i: number) => L + (i + 0.5) * ((R - L) / n);
  const Y = (v: number) => T + (1 - (v - lo) / (hi - lo || 1)) * (B - T);
  const cw = Math.max(3, ((R - L) / n) * 0.62);

  let grid = "";
  for (let k = 0; k <= 4; k++) {
    const yy = T + k * ((B - T) / 4);
    const pv = hi - (k / 4) * (hi - lo);
    grid += `<line x1="${L}" x2="${R}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" style="stroke:var(--border);stroke-width:.5"/><text x="${R + 4}" y="${(yy + 3).toFixed(1)}" style="fill:var(--text-muted);font-size:10px;font-family:var(--font-mono)">${pv.toFixed(0)}</text>`;
  }
  const z = Y(0);
  const zero = `<line x1="${L}" x2="${R}" y1="${z.toFixed(1)}" y2="${z.toFixed(1)}" style="stroke:var(--border-strong);stroke-width:1;stroke-dasharray:3 3"/>`;

  let xl = "";
  const step = Math.max(1, Math.round(n / 6));
  for (let i = 0; i < n; i += step) {
    xl += `<text x="${X(i).toFixed(1)}" y="${(B + 16).toFixed(1)}" text-anchor="middle" style="fill:var(--text-muted);font-size:9px;font-family:var(--font-mono)">${months[i].month}</text>`;
  }

  let cnd = "";
  for (let i = 0; i < n; i++) {
    const m = months[i];
    const up = m.close >= m.open;
    const col = up ? "var(--text-success)" : "var(--text-danger)";
    const cx = X(i);
    const yt = Y(Math.max(m.open, m.close));
    const yb = Y(Math.min(m.open, m.close));
    cnd += `<line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${Y(m.high).toFixed(1)}" y2="${Y(m.low).toFixed(1)}" style="stroke:${col};stroke-width:1;opacity:.8"/><rect x="${(cx - cw / 2).toFixed(1)}" y="${yt.toFixed(1)}" width="${cw.toFixed(1)}" height="${Math.max(1.5, yb - yt).toFixed(1)}" rx="1" style="fill:${col}"/>`;
  }

  const svg = `<svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%">${grid}${zero}${xl}${cnd}</svg>`;
  return <div dangerouslySetInnerHTML={{ __html: svg }} />;
}
