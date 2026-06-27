import { useMemo, useRef, useState } from "react";
import type { Bar } from "../api/types";
import { fnum } from "../lib/format";

const VB = 600,
  CL = 6,
  CR = 560;
const fin = (v: number | null | undefined): v is number => v != null && !Number.isNaN(v);

// Candlestick + Bollinger (upper/mid/lower) + MA fast/slow + weekly SMA10, with
// synced RSI, MACD and Volume panes, plus a live indicator legend. SVG strings
// interpolate ONLY numeric/server values (safe to inject).
export function PriceChart({ bars }: { bars: Bar[] }) {
  const cwRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const m = useMemo(() => buildModel(bars), [bars]);
  if (!m) return <p className="note">No data to chart.</p>;
  const { n, dts, cs, lg } = m;

  const onMove = (e: React.MouseEvent) => {
    const el = cwRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const ratio = (e.clientX - r.left) / r.width;
    let i = Math.round((ratio * VB - CL) / ((CR - CL) / (n - 1)));
    i = Math.max(0, Math.min(n - 1, i));
    setHover(i);
  };

  const hoverPx = hover == null ? 0 : ((CL + hover * ((CR - CL) / (n - 1))) / VB) * 100;
  const c = hover == null ? null : cs[hover];
  const tipRight = hoverPx > 58;

  const dot = (col: string) => <span style={{ color: col, marginRight: 4 }}>●</span>;

  return (
    <>
      <div className="chlegend">
        <span>{dot("var(--text-accent)")}MA50 {fnum(lg.maF, 2)}</span>
        <span>{dot("var(--text-warning)")}MA200 {fnum(lg.maS, 2)}</span>
        <span>{dot("var(--c-wsma)")}W-SMA10 {fnum(lg.wsma, 2)}</span>
        <span className="mut">BB {fnum(lg.bbU, 2)} / {fnum(lg.bbL, 2)}</span>
        <span>RSI {fnum(lg.rsi, 0)}</span>
        <span>ATR% {fnum(lg.atrPct, 2)}</span>
      </div>
      <div className="cw" ref={cwRef} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <div dangerouslySetInnerHTML={{ __html: m.candlesSvg }} />
        <div className="xh" style={{ left: hoverPx + "%", opacity: hover == null ? 0 : 0.6 }} />
        {c && (
          <div
            className="tip"
            style={{
              opacity: 1,
              left: tipRight ? "auto" : hoverPx + "%",
              right: tipRight ? 100 - hoverPx + "%" : "auto",
            }}
          >
            <span style={{ fontWeight: 500 }}>{dts[hover!]}</span> &nbsp;O {c[0].toFixed(2)} · H{" "}
            {c[1].toFixed(2)} · L {c[2].toFixed(2)} · C {c[3].toFixed(2)}
          </div>
        )}
      </div>
      <div style={{ marginTop: 4 }} dangerouslySetInnerHTML={{ __html: m.volSvg }} />
      <div style={{ marginTop: 4 }} dangerouslySetInnerHTML={{ __html: m.rsiSvg }} />
      <div style={{ marginTop: 4 }} dangerouslySetInnerHTML={{ __html: m.macdSvg }} />
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
interface Model {
  n: number;
  dts: string[];
  cs: number[][];
  lg: Legend;
  candlesSvg: string;
  volSvg: string;
  rsiSvg: string;
  macdSvg: string;
}

function buildModel(bars: Bar[]): Model | null {
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
  const rsi = bars.map((b) => b.rsi);
  const macd = bars.map((b) => b.macd);
  const sig = bars.map((b) => b.macd_signal);
  const hist = bars.map((b) => b.macd_hist);

  const Tp = 10,
    B = 184;
  const all = cs.flat().concat(bu.filter(fin), bl.filter(fin), wsma.filter(fin));
  const mn = Math.min(...all),
    mx = Math.max(...all),
    pd = (mx - mn) * 0.05,
    lo = mn - pd,
    hi = mx + pd;
  const X = (i: number) => CL + i * ((CR - CL) / (n - 1));
  const cw = ((CR - CL) / n) * 0.6;
  const Y = (v: number) => Tp + (1 - (v - lo) / (hi - lo || 1)) * (B - Tp);
  const fpts = (a: (number | null)[]) =>
    a.map((v, i) => (fin(v) ? `${X(i).toFixed(1)},${Y(v).toFixed(1)}` : null)).filter(Boolean).join(" ");
  const ln = (a: (number | null)[], col: string, w: number, dash = "0") =>
    `<polyline points="${fpts(a)}" style="fill:none;stroke:${col};stroke-width:${w};stroke-dasharray:${dash}"/>`;

  let grid = "",
    yl = "";
  for (let k = 0; k <= 4; k++) {
    const yy = Tp + k * ((B - Tp) / 4),
      pv = hi - (k / 4) * (hi - lo);
    grid += `<line x1="${CL}" x2="${CR}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" style="stroke:var(--border);stroke-width:.5"/>`;
    yl += `<text x="${CR + 4}" y="${(yy + 3).toFixed(1)}" style="fill:var(--text-muted);font-size:10px;font-family:var(--font-mono)">${pv.toFixed(0)}</text>`;
  }
  let xl = "";
  for (let k = 0; k <= 4; k++) {
    const i = Math.round((k * (n - 1)) / 4);
    xl += `<line x1="${X(i).toFixed(1)}" x2="${X(i).toFixed(1)}" y1="${Tp}" y2="${B}" style="stroke:var(--border);stroke-width:.5;opacity:.5"/><text x="${X(i).toFixed(1)}" y="${(B + 13).toFixed(1)}" text-anchor="middle" style="fill:var(--text-muted);font-size:10px;font-family:var(--font-mono)">${dts[i]}</text>`;
  }
  const bbA = fin(bu[0])
    ? "M" +
      bu.map((v, i) => `${X(i).toFixed(1)},${Y(v as number).toFixed(1)}`).join(" L") +
      " L" +
      bl.map((_, i) => `${X(n - 1 - i).toFixed(1)},${Y(bl[n - 1 - i] as number).toFixed(1)}`).join(" L") +
      "Z"
    : "";
  let cnd = "";
  for (let i = 0; i < n; i++) {
    const [o, h, l, cc] = cs[i],
      up = cc >= o,
      col = up ? "var(--text-success)" : "var(--text-danger)",
      cx = X(i),
      yt = Y(Math.max(o, cc)),
      yb = Y(Math.min(o, cc));
    cnd += `<line x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${Y(h).toFixed(1)}" y2="${Y(l).toFixed(1)}" style="stroke:${col};stroke-width:.8"/><rect x="${(cx - cw / 2).toFixed(1)}" y="${yt.toFixed(1)}" width="${cw.toFixed(1)}" height="${Math.max(0.8, yb - yt).toFixed(1)}" style="fill:${col}"/>`;
  }
  const candlesSvg = `<svg viewBox="0 0 ${VB} 210" width="100%">${grid}${xl}${bbA ? `<path d="${bbA}" style="fill:var(--bg-accent);opacity:.16"/>` : ""}${cnd}${ln(bu, "var(--text-muted)", 0.6)}${ln(bl, "var(--text-muted)", 0.6)}${ln(bm, "var(--text-muted)", 0.7, "2 3")}${ln(mas, "var(--text-warning)", 1.3)}${ln(maf, "var(--text-accent)", 1.3)}${ln(wsma, "var(--c-wsma)", 1.2)}${yl}</svg>`;

  // ── volume pane ──
  const vT = 8,
    vB = 46,
    vMax = Math.max(...vol.filter(fin), 1),
    VY = (v: number) => vT + (1 - v / vMax) * (vB - vT);
  let vb = "";
  for (let i = 0; i < n; i++) {
    if (!fin(vol[i])) continue;
    const col = cs[i][3] >= cs[i][0] ? "var(--text-success)" : "var(--text-danger)";
    const y = VY(vol[i] as number);
    vb += `<rect x="${(X(i) - cw / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${cw.toFixed(1)}" height="${Math.max(0.6, vB - y).toFixed(1)}" style="fill:${col};opacity:.5"/>`;
  }
  const volSvg = `<svg viewBox="0 0 ${VB} 56" width="100%">${vb}<text x="${CL}" y="11" style="fill:var(--text-muted);font-size:10px">Volume</text></svg>`;

  // ── RSI pane ──
  const rT = 10,
    rB = 54,
    RY = (v: number) => rT + (1 - v / 100) * (rB - rT);
  let rg = "";
  [70, 50, 30].forEach((v) => {
    rg += `<line x1="${CL}" x2="${CR}" y1="${RY(v).toFixed(1)}" y2="${RY(v).toFixed(1)}" style="stroke:var(--border);stroke-width:.5;stroke-dasharray:${v === 50 ? "0" : "2 3"}"/><text x="${CR + 4}" y="${(RY(v) + 3).toFixed(1)}" style="fill:var(--text-muted);font-size:9px;font-family:var(--font-mono)">${v}</text>`;
  });
  const rfpts = (a: (number | null)[]) =>
    a.map((v, i) => (fin(v) ? `${X(i).toFixed(1)},${RY(v).toFixed(1)}` : null)).filter(Boolean).join(" ");
  const rsiSvg = `<svg viewBox="0 0 ${VB} 66" width="100%">${rg}<polyline points="${rfpts(rsi)}" style="fill:none;stroke:var(--text-accent);stroke-width:1.2"/><text x="${CL}" y="11" style="fill:var(--text-muted);font-size:10px">RSI 14</text></svg>`;

  // ── MACD pane ──
  const mA = Math.max(...macd.filter(fin).map(Math.abs), ...sig.filter(fin).map(Math.abs), 0.01),
    mT = 12,
    mB = 56,
    MY = (v: number) => mT + (1 - (v / mA + 1) / 2) * (mB - mT),
    mw = cw;
  let mh = "";
  for (let i = 0; i < n; i++) {
    if (!fin(hist[i])) continue;
    const z = MY(0),
      y = MY(hist[i] as number),
      col = (hist[i] as number) >= 0 ? "var(--text-success)" : "var(--text-danger)";
    mh += `<rect x="${(X(i) - mw / 2).toFixed(1)}" y="${Math.min(z, y).toFixed(1)}" width="${mw.toFixed(1)}" height="${Math.max(0.6, Math.abs(z - y)).toFixed(1)}" style="fill:${col};opacity:.6"/>`;
  }
  const mfpts = (a: (number | null)[]) =>
    a.map((v, i) => (fin(v) ? `${X(i).toFixed(1)},${MY(v).toFixed(1)}` : null)).filter(Boolean).join(" ");
  const macdSvg = `<svg viewBox="0 0 ${VB} 68" width="100%"><line x1="${CL}" x2="${CR}" y1="${MY(0).toFixed(1)}" y2="${MY(0).toFixed(1)}" style="stroke:var(--border);stroke-width:.5"/>${mh}<polyline points="${mfpts(macd)}" style="fill:none;stroke:var(--text-accent);stroke-width:1.2"/><polyline points="${mfpts(sig)}" style="fill:none;stroke:var(--text-warning);stroke-width:1.2"/><text x="${CL}" y="11" style="fill:var(--text-muted);font-size:10px">MACD 12 26 9</text></svg>`;

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

  return { n, dts, cs, lg, candlesSvg, volSvg, rsiSvg, macdSvg };
}
