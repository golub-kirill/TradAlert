import { useEffect, useState } from "react";
import { getChart, getPositions } from "../api/client";
import { tickerOk } from "../lib/format";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { Card, Note } from "../components/Card";
import { PriceChart } from "../components/PriceChart";

export function Charts() {
  const toast = useToast();
  const positions = useApi(getPositions, []);
  const held = Array.from(new Set((positions.data ?? []).map((p) => p.ticker)));
  const chips = held.length ? [...held, "SPY"] : ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"];

  const [ticker, setTicker] = useState("");
  const [input, setInput] = useState("");

  // Default to the first held ticker once positions load (SPY fallback).
  useEffect(() => {
    if (!ticker && positions.data) setTicker(held[0] || "SPY");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions.data]);

  const active = ticker || "SPY";
  const c = useApi(() => getChart(active, 120), [active]);

  const load = () => {
    const t = input.trim();
    if (tickerOk(t)) {
      setTicker(t.toUpperCase());
      setInput("");
    } else {
      toast("Enter a valid ticker (e.g. AAPL).");
    }
  };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") load();
  };

  return (
    <>
      <div className="tabs">
        {chips.map((t) => (
          <button
            key={t}
            className={"chip" + (active === t ? " on" : "")}
            onClick={() => setTicker(t)}
          >
            {t}
          </button>
        ))}
        <span style={{ flex: 1 }} />
        <span className="chsearch">
          <i className="ti ti-search" />
          <input
            placeholder="Search ticker…"
            value={input}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInput(e.target.value)}
            onKeyDown={onKey}
          />
        </span>
        <button className="btn" onClick={load}>
          Load
        </button>
      </div>

      <Card title={active + " · daily"} icon="ti-chart-candle">
        {c.loading ? (
          <Note>Loading…</Note>
        ) : c.error || !c.data ? (
          <Note>No cached data for {active}.</Note>
        ) : (
          <PriceChart bars={c.data.bars} />
        )}
      </Card>
    </>
  );
}
