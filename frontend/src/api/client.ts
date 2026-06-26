// Typed client for the TradAlert control API. Same-origin "/api" (FastAPI serves
// the built SPA at "/"); in dev, Vite proxies /api to :8000.
import type {
  BacktestRun,
  BacktestRunReq,
  BacktestTrade,
  ChartData,
  ConfigResponse,
  EquityCurve,
  FiredSignal,
  Health,
  JobRef,
  JobStatus,
  OkResult,
  Position,
  ScannerLatest,
  ScanRun,
} from "./types";

const BASE = "/api";
const TOKEN_KEY = "tradalert_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}
export function setToken(t: string): void {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers as Record<string, string>) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["X-API-Token"] = token;
  const res = await fetch(BASE + path, { ...opts, headers });
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const d = (await res.json()) as { detail?: string };
      detail = d?.detail;
    } catch {
      /* non-json error body */
    }
    throw new ApiError(res.status, detail || `${res.status} ${res.statusText}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const body = (v: unknown): RequestInit => ({ body: JSON.stringify(v) });

// ---- read ----
export const getHealth = () => request<Health>("/health");
export const getPositions = () => request<Position[]>("/positions");
export const getScannerRuns = (limit = 25) => request<ScanRun[]>(`/scanner/runs?limit=${limit}`);
export const getScannerLatest = () => request<ScannerLatest>("/scanner/latest");
export const getBacktests = (limit = 20) => request<BacktestRun[]>(`/backtests?limit=${limit}`);
export const getBacktestTrades = (id: number, limit = 500) =>
  request<BacktestTrade[]>(`/backtests/${id}/trades?limit=${limit}`);
export const getEquity = (id: number) => request<EquityCurve>(`/backtests/${id}/equity`);
export const getChart = (ticker: string, days = 160) =>
  request<ChartData>(`/charts/${encodeURIComponent(ticker)}?days=${days}`);
export const getConfig = () => request<ConfigResponse>("/config");

// ---- jobs / actions ----
export const runBacktest = (req: BacktestRunReq) =>
  request<JobRef>("/backtests/run", { method: "POST", ...body(req) });
export const getJob = (jid: string) => request<JobStatus>(`/backtests/jobs/${jid}`);

// Live job tail via SSE (GET /api/backtests/jobs/{jid}/stream). Returns a stop fn.
// onLine receives each new output line; onStatus fires on terminal status. If the
// stream drops mid-run, it falls back to polling getJob so the UI never hangs on
// "running" (the job keeps running server-side).
export function streamJob(
  jid: string,
  onLine: (line: string) => void,
  onStatus: (status: JobStatus["status"]) => void,
): () => void {
  let closed = false;
  let pollTimer: number | undefined;
  const es = new EventSource(`${BASE}/backtests/jobs/${jid}/stream`);

  const finish = (s: JobStatus["status"]) => {
    if (closed) return;
    closed = true;
    es.close();
    if (pollTimer) window.clearTimeout(pollTimer);
    onStatus(s);
  };

  es.addEventListener("line", (e) => {
    if (!closed) onLine((e as MessageEvent).data);
  });
  es.addEventListener("status", (e) => {
    if (closed) return;
    const s = (e as MessageEvent).data as JobStatus["status"];
    if (s === "running") onStatus(s);
    else finish(s);
  });
  es.onerror = () => {
    if (closed) return;
    es.close(); // stop the auto-reconnect; poll the job to a terminal state instead
    const poll = async () => {
      if (closed) return;
      try {
        const st = await getJob(jid);
        if (st.status !== "running") return finish(st.status);
      } catch {
        /* transient — keep polling */
      }
      pollTimer = window.setTimeout(poll, 1500);
    };
    void poll();
  };

  return () => {
    closed = true;
    es.close();
    if (pollTimer) window.clearTimeout(pollTimer);
  };
}

// ---- position mutations (journal-only) ----
export interface OpenBody {
  ticker: string;
  entry_price: number;
  side?: string;
  stop_price?: number | null;
  entry_date?: string | null;
  notes?: string;
}
export const openPosition = (b: OpenBody) =>
  request<OkResult>("/positions", { method: "POST", ...body(b) });
export const updateStop = (id: number, stop_price: number) =>
  request<OkResult>(`/positions/${id}/stop`, { method: "PATCH", ...body({ stop_price }) });
export const closePosition = (id: number, exit_price: number, exit_date?: string) =>
  request<OkResult>(`/positions/${id}/close`, { method: "POST", ...body({ exit_price, exit_date }) });
export const scaleOut = (id: number, exit_price: number, fraction: number, exit_date?: string) =>
  request<OkResult>(`/positions/${id}/scale-out`, {
    method: "POST",
    ...body({ exit_price, fraction, exit_date }),
  });
export interface EditBody {
  entry_price?: number;
  stop_price?: number;
  initial_stop?: number;
  exit_price?: number;
  notes?: string;
}
export const editPosition = (id: number, b: EditBody) =>
  request<OkResult>(`/positions/${id}`, { method: "PATCH", ...body(b) });

// ---- config write + live scan ----
// Updates are dotted keys -> value, restricted server-side to a whitelist of
// operational knobs (see GET /config `editable`). Locked keys are rejected.
export const saveConfig = (updates: Record<string, number | boolean | string>) =>
  request<{ ok: boolean; written: string[] }>("/config", { method: "POST", ...body({ updates }) });
export const runScan = (opts: { morning?: boolean; force?: boolean } = {}) =>
  request<JobRef>("/scan", { method: "POST", ...body(opts) });

export type { FiredSignal };
