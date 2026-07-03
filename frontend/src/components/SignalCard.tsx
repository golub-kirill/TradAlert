import type {FiredSignal} from "../api/types";
import {fnum} from "../lib/format";

// open/buy = green, hold (entry on a name you already hold) = blue, exit/sell = red.
type Side = "buy" | "hold" | "sell";

function sideOf(f: FiredSignal, held: boolean): Side {
  if ((f.signal_kind || "").startsWith("exit")) return "sell";
  return held ? "hold" : "buy";
}

function rr(f: FiredSignal): number | null {
  if (f.close == null || f.stop_price == null || f.target_price == null) return null;
  const risk = Math.abs(f.close - f.stop_price);
  if (!risk) return null;
  return Math.abs(f.target_price - f.close) / risk;
}

const CHIP: Record<Side, string> = { buy: "Buy", hold: "Hold", sell: "Sell" };

const TYPE_LABEL: Record<string, string> = {
  regime: "Regime exit",
  momentum: "Momentum",
  mean_reversion: "Mean reversion",
  time_stop: "Time stop",
  pead: "Earnings drift",
};

export function SignalCard({
  f,
  held,
  busy,
  onOpen,
  onClose,
}: {
  f: FiredSignal;
  held: boolean;
  busy: boolean;
  onOpen: (f: FiredSignal) => void;
  onClose: (f: FiredSignal) => void;
}) {
  const side = sideOf(f, held);
  const isExit = side === "sell";
  const chip = isExit && f.signal_kind === "exit_short" ? "Cover" : CHIP[side];
  const typeLabel =
    TYPE_LABEL[f.signal_type || ""] ||
    (f.signal_type ? f.signal_type.replaceAll("_", " ") : f.signal_kind.replaceAll("_", " "));
  const reason = isExit ? f.reason : f.review_reason;
  const ratio = rr(f);

  return (
    <div className={"scard " + side}>
      <div className="scard-top">
        <div style={{ minWidth: 0 }}>
          <div className="scard-tkr">{f.ticker}</div>
          <div className="scard-name">{f.name || typeLabel}</div>
        </div>
        <span className={"schip " + side}>{chip}</span>
      </div>

      <div className="statgrid">
        <div className="stat">
          <div className="k">Close</div>
          <div className="val">{fnum(f.close, 2)}</div>
        </div>
        <div className="stat">
          <div className="k">Stop</div>
          <div className="val">{fnum(f.stop_price, 2)}</div>
        </div>
        <div className="stat">
          <div className="k">Target</div>
          <div className="val">{fnum(f.target_price, 2)}</div>
        </div>
        <div className="stat">
          <div className="k">R:R</div>
          <div className="val">{ratio == null ? "—" : ratio.toFixed(2)}</div>
        </div>
      </div>

      {reason ? <div className="scard-reason">{reason}</div> : null}

        {f.advisor_note ? (
            <div className="scard-advisor">
                <i className="ti ti-robot"/>
                <span>{f.advisor_note}</span>
            </div>
        ) : null}

      <div className="scard-foot">
        <span className="scard-tier">
          <span className="stag">{typeLabel}</span>
          {f.tier === "NEEDS_REVIEW" ? <span className="badge b-rev">Review</span> : null}
        </span>
        {side === "buy" ? (
          <button className="btn success" disabled={busy || f.close == null} onClick={() => onOpen(f)}>
            <i className="ti ti-plus" />
            Open
          </button>
        ) : side === "hold" ? (
          <span className="scard-tier">
            <i className="ti ti-circle-check" />
            Holding
          </span>
        ) : held ? (
          <button className="btn danger" disabled={busy} onClick={() => onClose(f)}>
            <i className="ti ti-logout" />
            Close
          </button>
        ) : (
          <span className="mut" style={{ fontSize: 11 }}>
            flat
          </span>
        )}
      </div>
    </div>
  );
}
