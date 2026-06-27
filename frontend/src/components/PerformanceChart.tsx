import type { MonthlyBar } from "../api/types";

// Two-pane performance chart from monthly data:
//  • top  — cumulative-R equity curve (area + line)   [the "curve"]
//  • below — monthly net-R bars around zero (green win / red loss), well-scaled
// Only numeric/server values are interpolated into the SVG (safe to inject).
export function PerformanceChart({ months }: { months: MonthlyBar[] }) {
  if (months.length < 2) return <p className="note">Not enough trades to chart.</p>;

  const VB_W = 720,
    L = 10,
    R = 684;
  const eT = 12,
    eB = 214; // equity pane
  const bT = 238,
    bB = 312; // P&L pane
  const VB_H = 332;
  const n = months.length;

  const closes = months.map((m) => m.close);
  const eLo = Math.min(0, ...closes);
  const eHi = Math.max(...closes);
  const ePad = (eHi - eLo) * 0.06 || 1;
  const lo = eLo - ePad,
    hi = eHi + ePad;

  const X = (i: number) => L + i * ((R - L) / (n - 1));
  const EY = (v: number) => eT + (1 - (v - lo) / (hi - lo || 1)) * (eB - eT);

  const rMax = Math.max(...months.map((m) => Math.abs(m.r)), 0.01);
  const BY = (v: number) => bT + (1 - (v / rMax + 1) / 2) * (bB - bT); // 0 centred

  // equity grid + R labels
  let grid = "";
  for (let k = 0; k <= 4; k++) {
    const yy = eT + k * ((eB - eT) / 4);
    const pv = hi - (k / 4) * (hi - lo);
    grid += `<line x1="${L}" x2="${R}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" style="stroke:var(--border);stroke-width:.5"/><text x="${R + 4}" y="${(yy + 3).toFixed(1)}" style="fill:var(--text-muted);font-size:10px;font-family:var(--font-mono)">${pv.toFixed(0)}</text>`;
  }

  const pts = closes.map((v, i) => `${X(i).toFixed(1)},${EY(v).toFixed(1)}`).join(" ");
  const area =
    `M${X(0).toFixed(1)},${eB} L` +
    closes.map((v, i) => `${X(i).toFixed(1)},${EY(v).toFixed(1)}`).join(" L") +
    ` L${X(n - 1).toFixed(1)},${eB} Z`;

  // P&L bars
  const z = BY(0);
  const bw = Math.max(1.4, ((R - L) / n) * 0.72);
  let bars = `<line x1="${L}" x2="${R}" y1="${z.toFixed(1)}" y2="${z.toFixed(1)}" style="stroke:var(--border-strong);stroke-width:.6"/>`;
  for (let i = 0; i < n; i++) {
    const r = months[i].r;
    const y = BY(r);
    const col = r >= 0 ? "var(--text-success)" : "var(--text-danger)";
    bars += `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${Math.min(z, y).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.8, Math.abs(z - y)).toFixed(1)}" rx="0.5" style="fill:${col};opacity:.85"/>`;
  }

  // x labels (shared)
  let xl = "";
  const step = Math.max(1, Math.round(n / 7));
  for (let i = 0; i < n; i += step) {
    xl += `<text x="${X(i).toFixed(1)}" y="${(bB + 14).toFixed(1)}" text-anchor="middle" style="fill:var(--text-muted);font-size:9px;font-family:var(--font-mono)">${months[i].month}</text>`;
  }

  const svg =
    `<svg viewBox="0 0 ${VB_W} ${VB_H}" width="100%">` +
    `<defs><linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0%" stop-color="var(--accent)" stop-opacity="0.28"/>` +
    `<stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>` +
    grid +
    `<path d="${area}" fill="url(#eqfill)"/>` +
    `<polyline points="${pts}" style="fill:none;stroke:var(--text-accent);stroke-width:1.8"/>` +
    `<text x="${L}" y="${(eT + 9).toFixed(1)}" style="fill:var(--text-muted);font-size:10px">Equity · R</text>` +
    `<text x="${L}" y="${(bT - 4).toFixed(1)}" style="fill:var(--text-muted);font-size:10px">Monthly P&L · R</text>` +
    bars +
    xl +
    `</svg>`;

  return <div dangerouslySetInnerHTML={{ __html: svg }} />;
}
