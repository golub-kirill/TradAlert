import type { ComponentType } from "react";
import { Overview } from "./Overview";
import { Scanner } from "./Scanner";
import { Backtest } from "./Backtest";
import { Charts } from "./Charts";
import { Positions } from "./Positions";
import { Settings } from "./Settings";

export type ViewKey = "overview" | "scanner" | "backtest" | "charts" | "positions" | "settings";

export interface ViewDef {
  key: ViewKey;
  /** Deep-linkable URL. Overview is the deck root. */
  path: string;
  title: string;
  sub: string;
  icon: string;
  Component: ComponentType;
}

export const VIEWS: ViewDef[] = [
  {
    key: "overview",
    path: "/app",
    title: "Overview",
    sub: "Strategy performance and activity",
    icon: "ti-layout-dashboard",
    Component: Overview,
  },
  {
    key: "scanner",
    path: "/app/scanner",
    title: "Scanner",
    sub: "Latest watchlist scan",
    icon: "ti-radar",
    Component: Scanner,
  },
  {
    key: "backtest",
    path: "/app/backtest",
    title: "Backtest",
    sub: "Replay the engine over a date range",
    icon: "ti-flask",
    Component: Backtest,
  },
  {
    key: "charts",
    path: "/app/charts",
    title: "Charts",
    sub: "Price, indicators and signals",
    icon: "ti-chart-candle",
    Component: Charts,
  },
  {
    key: "positions",
    path: "/app/positions",
    title: "Positions",
    sub: "Edit held positions, live",
    icon: "ti-briefcase",
    Component: Positions,
  },
  {
    key: "settings",
    path: "/app/settings",
    title: "Settings",
    sub: "Filters, regime and risk",
    icon: "ti-settings",
    Component: Settings,
  },
];

/** Exact match only. Prefix matching would resolve /app/scaner to the /app root
 *  and silently serve Overview under a URL that does not exist, which also made
 *  the 404 page unreachable for the whole /app subtree. Returns undefined so the
 *  caller can fall through to NotFound. */
export function viewForPath(path: string): ViewDef | undefined {
  return VIEWS.find((v) => v.path === path);
}
