import { useEffect, useState } from "react";
import { getChart, getPositions } from "../api/client";
import { tickerOk } from "../lib/format";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { Card, Empty } from "../components/Card";
import { SkeletonBlock } from "../components/Skeleton";
import { PriceChart } from "../components/PriceChart";

// Benchmarks only when nothing is held — the chip row is a starting point, not
// a watchlist, so it never names individual holdings the panel doesn't track.
const BENCHMARKS = ["SPY", "QQQ"];

export function Charts() {
  const toast = useToast();
  const positions = useApi(getPositions, []);
  const held = Array.from(new Set((positions.data ?? []).map((p) => p.ticker)));
  const chips = held.length ? [...held, BENCHMARKS[0]] : BENCHMARKS;

  const [ticker, setTicker] = useState("");
  const [input, setInput] = useState("");

  // Default to the first held ticker once positions load (SPY fallback).
  useEffect(() => {
    if (!ticker && positions.data) setTicker(held[0] || BENCHMARKS[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions.data]);

  const active = ticker || BENCHMARKS[0];
  const c = useApi(() => getChart(active, 120), [active]);

  const load = () => {
    const t = input.trim();
    if (tickerOk(t)) {
      setTicker(t.toUpperCase());
      setInput("");
    } else {
      toast("Enter a valid ticker.", "error");
    }
  };

  return (
    <>
      <div className="tabbar">
        {chips.map((t) => (
          <button
            key={t}
            className="chip"
            aria-pressed={active === t}
            onClick={() => setTicker(t)}
          >
            {t}
          </button>
        ))}
        <span className="tabbar__spacer" />
        <span className="searchpill">
          <i className="ti ti-search" aria-hidden="true" />
          <input
            type="search"
            placeholder="Search ticker…"
            aria-label="Search for a ticker"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") load();
            }}
          />
        </span>
        <button className="btn" onClick={load}>
          Load
        </button>
      </div>

      <Card title={active + " · daily"} icon="ti-chart-candle">
        {c.loading ? (
          <SkeletonBlock height={280} />
        ) : c.error || !c.data || c.data.bars.length < 2 ? (
          <Empty icon="ti-chart-line">
            No cached bars for {active}. Fetch prices for it, then reload.
          </Empty>
        ) : (
          <PriceChart bars={c.data.bars} />
        )}
      </Card>
    </>
  );
}
