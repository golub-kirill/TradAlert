// Response shapes for the TradAlert control API (/api). Mirrors api/routers/*.

export interface Health {
  ok: boolean;
}

export interface Position {
  id: number;
  ticker: string;
  side: string; // "long" | "short"
  entry_price: number;
  entry_date: string;
  stop_price: number | null;
  current: number | null;
  unrealized_r: number | null;
}

export interface ScanRun {
  id: number;
  created_at: string;
  market_regime: string | null;
  tickers_scanned: number;
  scan_passed: number;
  signals_fired: number;
}

// latest_scan_run() keys the id as run_id (differs from /scanner/runs above).
export interface LatestRun {
  run_id: number;
  created_at: string;
  market_regime: string | null;
  tickers_scanned: number;
  scan_passed: number;
  signals_fired: number;
}

export interface FiredSignal {
  ticker: string;
  name: string | null; // full company name (UI enrichment)
  signal_kind: string; // entry_long | entry_short | exit_long | exit_short
  signal_type: string | null;
  close: number | null;
  stop_price: number | null;
  target_price: number | null;
  tier: string | null; // "LIVE" | "NEEDS_REVIEW"
  review_reason: string | null;
  reason: string | null; // per-ticker scoreboard / exit driver
}

export interface ScannerLatest {
  run: LatestRun | null;
  fired: FiredSignal[];
  stand_down: unknown | null;
}

export interface ParamDiff {
  key: string;
  value: unknown;
  default: unknown;
}
export interface BacktestRun {
  id: number;
  started_at: string;
  start_date: string | null;
  end_date: string | null;
  trades_count: number;
  total_r: number | null;
  expectancy_r: number | null;
  profit_factor: number | null;
  win_rate: number | null;
  max_drawdown_r: number | null;
  notes: string | null;
  params?: ParamDiff[]; // run params that differed from the shipped config
  window?: string | null; // date window from the run's _meta
}

export interface EquityPoint {
  date: string;
  equity_r: number;
}
export interface EquityCurve {
  run_id: number;
  points: EquityPoint[];
}

export interface MonthlyBar {
  month: string; // YYYY-MM
  open: number;
  high: number;
  low: number;
  close: number;
  r: number;
  wins: number;
  losses: number;
}
export interface MonthlyPerf {
  run_id: number;
  months: MonthlyBar[];
  win_rate: number | null;
  up_month_pct: number | null;
  wins: number;
  losses: number;
}

export interface BacktestTrade {
  ticker: string;
  direction: string;
  signal_type: string | null;
  entry_date: string | null;
  exit_date: string | null;
  exit_reason: string | null;
  r_multiple: number | null;
  effective_r: number | null;
  market_regime: string | null;
}

export interface Bar {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  atr: number | null;
  ma_fast: number | null;
  ma_slow: number | null;
  weekly_sma10: number | null;
  rsi: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_hist: number | null;
  bb_mid: number | null;
  bb_upper: number | null;
  bb_lower: number | null;
}

export interface ChartData {
  ticker: string;
  bars: Bar[];
}

export type ConfigSection = Record<string, unknown>;
export interface ConfigResponse {
  filters: ConfigSection;
  settings: ConfigSection;
}

export type BacktestMode = "baseline" | "sweep" | "walk-forward" | "robustness";

export interface BacktestRunReq {
  start?: string;
  end?: string;
  mode?: BacktestMode;
  max_open_risk?: number;
  breakeven_trigger_r?: number;
  max_hold_days?: number;
  max_hold_mode?: "if_not_profit" | "hard";
  trail_atr_mult?: number;
  allow_shorts?: boolean;
  chronic_penalty?: boolean;
  vix_slope_gate?: boolean;
  anti_gap_entry?: boolean;
  tickers?: string[] | null;
}

export interface JobRef {
  job_id: string;
  cmd: string;
}

export interface JobStatus {
  status: "running" | "done" | "error" | "unknown";
  returncode: number | null;
  cmd?: string;
  tail?: string[];
}

export interface OkResult {
  ok: boolean;
  id?: number;
  [k: string]: unknown;
}
