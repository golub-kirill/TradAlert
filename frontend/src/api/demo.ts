/* Generated sample data for two jobs:
 *   • the public landing page, which must never show a real journal
 *   • the panel's offline mode, so an unreachable API degrades to a readable
 *     dashboard instead of six empty cards
 *
 * Every symbol uses the TEST.* convention, so demo figures can never be mistaken
 * for live ones. Deterministic (seeded) so the page looks identical on reload
 * and screenshots stay stable.
 */

import type {
  BacktestRun,
  BacktestTrade,
  ChartData,
  ConfigResponse,
  EquityCurve,
  FiredSignal,
  MonthlyPerf,
  Position,
  ScanRun,
  ScannerLatest,
} from "./types";

/** mulberry32 — small, fast, and stable across engines. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const MONTHS = 96;
const START_YEAR = 2018;

/** A cumulative-R path with a fat right tail and real drawdowns. The monthly
 *  noise is deliberately large relative to the drift: a smooth line would be
 *  flattering and false, and this curve is the page's signature visual. */
function monthlySeries() {
  const r = rng(20260726);
  const out: Array<{ month: string; r: number; close: number; wins: number; losses: number }> = [];
  let cum = 0;
  for (let i = 0; i < MONTHS; i++) {
    const y = START_YEAR + Math.floor(i / 12);
    const m = (i % 12) + 1;
    const drift = 0.95;
    const shock = r() < 0.11 ? -(3 + r() * 5) : 0;
    const tail = r() < 0.07 ? 5 + r() * 7 : 0;
    const noise = (r() - 0.5) * 8;
    const net = +(drift + noise + shock + tail).toFixed(2);
    cum = +(cum + net).toFixed(2);
    const trades = 8 + Math.floor(r() * 14);
    const wins = Math.max(1, Math.round(trades * (0.34 + r() * 0.16)));
    out.push({
      month: `${y}-${String(m).padStart(2, "0")}`,
      r: net,
      close: cum,
      wins,
      losses: trades - wins,
    });
  }
  return out;
}

const SERIES = monthlySeries();
const TOTAL_R = SERIES[SERIES.length - 1].close;

/** Peak-to-trough of the cumulative path, in R. */
function maxDrawdown(): number {
  let peak = -Infinity;
  let dd = 0;
  for (const p of SERIES) {
    peak = Math.max(peak, p.close);
    dd = Math.max(dd, peak - p.close);
  }
  return +dd.toFixed(2);
}

const WINS = SERIES.reduce((s, m) => s + m.wins, 0);
const LOSSES = SERIES.reduce((s, m) => s + m.losses, 0);
const TRADES = WINS + LOSSES;

export const demoHeadline = {
  totalR: TOTAL_R,
  trades: TRADES,
  winRate: WINS / TRADES,
  expectancy: +(TOTAL_R / TRADES).toFixed(3),
  profitFactor: 1.74,
  maxDrawdownR: maxDrawdown(),
  window: `${SERIES[0].month} → ${SERIES[SERIES.length - 1].month}`,
  months: SERIES.length,
};

export const demoCurve = SERIES.map((m) => ({ label: m.month, value: m.close }));

/** Funnel counts for the landing pipeline section. */
export const demoPipeline = [
  { name: "Universe", value: 412, desc: "Watchlist symbols with fresh bars." },
  { name: "Liquidity + trend", value: 168, desc: "Hard filters on price, volume and structure." },
  { name: "Setup", value: 34, desc: "Momentum and mean-reversion triggers." },
  { name: "Regime + risk", value: 9, desc: "Market state and open-risk budget applied." },
  { name: "Journaled", value: 3, desc: "Signals written with stop, target and reason." },
];

const demoBacktests: BacktestRun[] = [
  {
    // Newest run is deliberately an A/B leg, so the panel's provenance guard is
    // visible in demo mode rather than only in the code.
    id: 41,
    started_at: "2026-07-24T22:14:00",
    start_date: "2018-01-01",
    end_date: "2025-12-31",
    trades_count: 1490,
    total_r: +(TOTAL_R * 0.52).toFixed(2),
    expectancy_r: 0.031,
    profit_factor: 1.28,
    win_rate: 0.39,
    max_drawdown_r: 51.2,
    notes: "vix_slope_gate=on",
    config_match: false,
    config_mismatch: ["filters.vix_slope_gate", "filters.max_open_risk"],
    window: "2018-01-01 → 2025-12-31",
  },
  {
    id: 40,
    started_at: "2026-07-23T09:02:00",
    start_date: SERIES[0].month + "-01",
    end_date: "2025-12-31",
    trades_count: TRADES,
    total_r: TOTAL_R,
    expectancy_r: demoHeadline.expectancy,
    profit_factor: demoHeadline.profitFactor,
    win_rate: demoHeadline.winRate,
    max_drawdown_r: demoHeadline.maxDrawdownR,
    notes: "baseline",
    config_match: true,
    window: demoHeadline.window,
  },
];

const demoPositions: Position[] = [
  { id: 1, ticker: "TEST.1", side: "long", entry_price: 42.18, entry_date: "2026-07-09", stop_price: 39.4, current: 46.02, unrealized_r: 1.38 },
  { id: 2, ticker: "TEST.2", side: "long", entry_price: 118.6, entry_date: "2026-07-15", stop_price: 111.2, current: 121.35, unrealized_r: 0.37 },
  { id: 3, ticker: "TEST.3", side: "short", entry_price: 27.9, entry_date: "2026-07-18", stop_price: 30.1, current: 28.44, unrealized_r: -0.25 },
  { id: 4, ticker: "TEST.4", side: "long", entry_price: 9.44, entry_date: "2026-07-21", stop_price: 8.71, current: 10.98, unrealized_r: 2.11 },
];

const demoFired: FiredSignal[] = [
  {
    ticker: "TEST.5",
    name: "Test Industrials Corp",
    signal_kind: "entry_long",
    signal_type: "momentum",
    close: 63.4,
    stop_price: 58.9,
    target_price: 76.6,
    tier: "LIVE",
    review_reason: "trend 3/3 · volume 1.8× · RS 0.91 · above weekly SMA10",
    advisor_note: "No contradicting headlines in the last 5 sessions. Earnings 34 days out.",
    reason: null,
  },
  {
    ticker: "TEST.6",
    name: "Test Materials Ltd",
    signal_kind: "entry_long",
    signal_type: "mean_reversion",
    close: 21.05,
    stop_price: 19.6,
    target_price: 24.9,
    tier: "NEEDS_REVIEW",
    review_reason: "band touch · RSI 28 · trend intact, but sector breadth is thin",
    advisor_note: null,
    reason: null,
  },
  {
    ticker: "TEST.3",
    name: "Test Energy Partners",
    signal_kind: "exit_short",
    signal_type: "regime",
    close: 28.44,
    stop_price: null,
    target_price: null,
    tier: "LIVE",
    review_reason: null,
    advisor_note: null,
    reason: "regime flipped BULL — short leg stands down",
  },
];

const demoRun: ScannerLatest["run"] = {
  run_id: 1284,
  created_at: "2026-07-26T07:31:00",
  market_regime: "BULL",
  tickers_scanned: 412,
  scan_passed: 34,
  signals_fired: 3,
};

const demoScanRuns: ScanRun[] = Array.from({ length: 12 }, (_, i) => {
  const r = rng(900 + i);
  return {
    id: 1284 - i,
    created_at: `2026-07-${String(26 - i).padStart(2, "0")}T07:31:00`,
    market_regime: r() > 0.25 ? "BULL" : r() > 0.1 ? "CHOP" : "BEAR",
    tickers_scanned: 412,
    scan_passed: 20 + Math.floor(r() * 30),
    signals_fired: Math.floor(r() * 5),
  };
});

function demoChart(ticker: string): ChartData {
  const r = rng(7 + ticker.length);
  const bars: ChartData["bars"] = [];
  let price = 40 + r() * 30;
  const closes: number[] = [];
  for (let i = 0; i < 160; i++) {
    const d = new Date(Date.UTC(2026, 1, 1));
    d.setUTCDate(d.getUTCDate() + i);
    price = Math.max(3, price * (1 + (r() - 0.47) * 0.032));
    closes.push(price);
    const ma = (n: number) =>
      closes.length >= n ? closes.slice(-n).reduce((s, v) => s + v, 0) / n : null;
    const high = price * (1 + r() * 0.012);
    const low = price * (1 - r() * 0.012);
    bars.push({
      date: d.toISOString().slice(0, 10),
      open: +(price * (1 + (r() - 0.5) * 0.008)).toFixed(2),
      high: +high.toFixed(2),
      low: +low.toFixed(2),
      close: +price.toFixed(2),
      volume: Math.round(400_000 + r() * 2_400_000),
      atr: +(price * 0.021).toFixed(3),
      ma_fast: ma(20),
      ma_slow: ma(50),
      weekly_sma10: ma(50),
      rsi: +(38 + r() * 30).toFixed(1),
      macd: +((r() - 0.5) * 1.4).toFixed(3),
      macd_signal: +((r() - 0.5) * 1.2).toFixed(3),
      macd_hist: +((r() - 0.5) * 0.6).toFixed(3),
      bb_mid: ma(20),
      bb_upper: ma(20) != null ? +(ma(20)! * 1.05).toFixed(2) : null,
      bb_lower: ma(20) != null ? +(ma(20)! * 0.95).toFixed(2) : null,
    });
  }
  return { ticker, bars };
}

const demoTrades: BacktestTrade[] = Array.from({ length: 40 }, (_, i) => {
  const r = rng(1200 + i);
  const win = r() > 0.61;
  return {
    ticker: `TEST.${(i % 8) + 1}`,
    direction: r() > 0.15 ? "long" : "short",
    signal_type: r() > 0.5 ? "momentum" : "mean_reversion",
    entry_date: `2025-${String(((i * 3) % 12) + 1).padStart(2, "0")}-0${(i % 8) + 1}`,
    exit_date: `2025-${String(((i * 3) % 12) + 1).padStart(2, "0")}-1${(i % 8) + 1}`,
    exit_reason: win ? "target" : r() > 0.5 ? "stop" : "time_stop",
    r_multiple: win ? +(1 + r() * 5).toFixed(2) : +(-0.4 - r() * 0.7).toFixed(2),
    effective_r: null,
    market_regime: r() > 0.3 ? "BULL" : "CHOP",
  };
});

const demoMonthly: MonthlyPerf = {
  run_id: 40,
  months: SERIES.map((m) => ({
    month: m.month,
    open: m.close - m.r,
    high: Math.max(m.close, m.close - m.r),
    low: Math.min(m.close, m.close - m.r),
    close: m.close,
    r: m.r,
    wins: m.wins,
    losses: m.losses,
  })),
  win_rate: demoHeadline.winRate,
  up_month_pct: SERIES.filter((m) => m.r > 0).length / SERIES.length,
  wins: WINS,
  losses: LOSSES,
};

const demoEquity: EquityCurve = {
  run_id: 40,
  points: SERIES.map((m) => ({ date: `${m.month}-01`, equity_r: m.close })),
};

/** Shaped like the real GET /config: nested sections plus the server's whitelist
 *  of writable dotted keys. Settings reads by dotted path, so a flat stub would
 *  render every row as "—" and make the view look broken in demo mode. */
const demoConfig: ConfigResponse & { editable: string[] } = {
  filters: {
    price: { min_price: 5 },
    liquidity: { min_dollar_volume_20d: 5_000_000 },
    volatility: { min_atr_pct: 1.5, max_atr_pct: 9 },
    trend: { ma_fast: 50, ma_slow: 200 },
    signals: {
      allow_shorts: false,
      sector_gate: { enabled: true },
      stop_loss: { min_rr: 2, atr_multiplier: 2.5 },
    },
    execution: { max_hold_days: 25, max_hold_mode: "if_not_profit", breakeven_trigger_r: 1 },
    regime: { vix_low: 15, vix_high: 28 },
  },
  settings: {
    macro: { enabled: true },
    behavioral: { enabled: false },
    risk: { max_open_risk: 5 },
    scanner: { event_risk_within_days: 3 },
    telegram: { enabled: true, send_stand_down: true },
  },
  editable: [
    "filters.price.min_price",
    "filters.liquidity.min_dollar_volume_20d",
    "filters.volatility.min_atr_pct",
    "filters.volatility.max_atr_pct",
    "filters.trend.ma_fast",
    "filters.trend.ma_slow",
    "filters.signals.stop_loss.min_rr",
    "filters.signals.stop_loss.atr_multiplier",
    "filters.execution.max_hold_days",
    "filters.execution.breakeven_trigger_r",
    "filters.regime.vix_low",
    "filters.regime.vix_high",
    "filters.signals.allow_shorts",
    "filters.signals.sector_gate.enabled",
    "settings.macro.enabled",
    "settings.behavioral.enabled",
    "settings.risk.max_open_risk",
    "settings.scanner.event_risk_within_days",
    "settings.telegram.enabled",
    "settings.telegram.send_stand_down",
  ],
};

/** Fixture for a GET path, or null when that endpoint has no demo counterpart
 *  (health deliberately has none — it must keep failing so the panel knows it
 *  is offline and labels the data as demo). */
export function demoFor(path: string): unknown | null {
  const p = path.split("?")[0];
  if (p === "/positions") return demoPositions;
  if (p === "/scanner/latest") return { run: demoRun, fired: demoFired, stand_down: null } satisfies ScannerLatest;
  if (p === "/scanner/runs") return demoScanRuns;
  if (p === "/backtests") return demoBacktests;
  if (p === "/config") return demoConfig;
  if (/^\/backtests\/\d+\/monthly$/.test(p)) return demoMonthly;
  if (/^\/backtests\/\d+\/equity$/.test(p)) return demoEquity;
  if (/^\/backtests\/\d+\/trades$/.test(p)) return demoTrades;
  const chart = p.match(/^\/charts\/(.+)$/);
  if (chart) return demoChart(decodeURIComponent(chart[1]));
  return null;
}
