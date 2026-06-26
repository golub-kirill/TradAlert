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
  title: string;
  sub: string;
  icon: string;
  Component: ComponentType;
}

export const VIEWS: ViewDef[] = [
  { key: "overview", title: "Overview", sub: "Strategy performance and activity", icon: "ti-layout-dashboard", Component: Overview },
  { key: "scanner", title: "Scanner", sub: "Latest watchlist scan", icon: "ti-radar", Component: Scanner },
  { key: "backtest", title: "Backtest", sub: "Replay the engine over a date range", icon: "ti-flask", Component: Backtest },
  { key: "charts", title: "Charts", sub: "Price, indicators and signals", icon: "ti-chart-candle", Component: Charts },
  { key: "positions", title: "Positions", sub: "Edit held positions, live", icon: "ti-briefcase", Component: Positions },
  { key: "settings", title: "Settings", sub: "Filters, regime and risk", icon: "ti-settings", Component: Settings },
];
