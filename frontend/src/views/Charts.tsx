import { useState } from "react";
import { getChart } from "../api/client";
import { tickerOk } from "../lib/format";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { Card, Note } from "../components/Card";
import { PriceChart } from "../components/PriceChart";

const PRESETS = ["SPY", "AAPL", "MSFT", "NVDA", "QQQ"];

export function Charts() {
  const toast = useToast();
  const [ticker, setTicker] = useState("SPY");
  const [input, setInput] = useState("");

  const c = useApi(() => getChart(ticker, 80), [ticker]);

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
        {PRESETS.map((t) => (
          <button
            key={t}
            className={"chip" + (ticker === t ? " on" : "")}
            onClick={() => setTicker(t)}
          >
            {t}
          </button>
        ))}
        <input
          className="fld"
          placeholder="Ticker…"
          value={input}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInput(e.target.value)}
          onKeyDown={onKey}
          style={{ width: 110 }}
        />
        <button className="btn" onClick={load}>
          Load
        </button>
      </div>

      <Card title={ticker + " · daily"} icon="ti-chart-candle">
        {c.loading ? (
          <Note>Loading…</Note>
        ) : c.error || !c.data ? (
          <Note>No cached data for {ticker}.</Note>
        ) : (
          <PriceChart bars={c.data.bars} />
        )}
      </Card>
    </>
  );
}
