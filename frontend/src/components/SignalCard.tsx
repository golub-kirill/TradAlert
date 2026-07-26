import type { ReactNode } from "react";
import type { FiredSignal } from "../api/types";
import { fnum } from "../lib/format";

// open/buy = lime rail, hold (entry on a name you already hold) = neutral rail,
// exit/sell = coral rail. Colour confirms the state; the glyph and label carry
// it, so nothing depends on hue alone.
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
const GLYPH: Record<Side, string> = {
  buy: "ti-arrow-up-right",
  hold: "ti-minus",
  sell: "ti-arrow-down-right",
};

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
  confirm,
}: {
  f: FiredSignal;
  held: boolean;
  busy: boolean;
  onOpen: (f: FiredSignal) => void;
  onClose: (f: FiredSignal) => void;
  /** Confirmation prompt for a pending action on THIS signal. Rendered inside
   *  the card so the question appears where the button was pressed. */
  confirm?: ReactNode;
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
    <article className={"signal signal--" + side}>
      <div className="signal__top">
        <div style={{ minWidth: 0 }}>
          <h3 className="signal__ticker">{f.ticker}</h3>
          <p className="signal__name">{f.name || typeLabel}</p>
        </div>
        <span className={"signal__chip signal__chip--" + side}>
          <i className={"ti " + GLYPH[side]} aria-hidden="true" />
          {chip}
        </span>
      </div>

      <div className="signal__stats">
        <div>
          <div className="signal__k">Close</div>
          <div className="signal__v">{fnum(f.close, 2)}</div>
        </div>
        <div>
          <div className="signal__k">Stop</div>
          <div className="signal__v">{fnum(f.stop_price, 2)}</div>
        </div>
        <div>
          <div className="signal__k">Target</div>
          <div className="signal__v">{fnum(f.target_price, 2)}</div>
        </div>
        <div>
          <div className="signal__k">R:R</div>
          <div className="signal__v">{ratio == null ? "—" : ratio.toFixed(2)}</div>
        </div>
      </div>

      {reason ? <p className="signal__reason">{reason}</p> : null}

      {f.advisor_note ? (
        <p className="signal__advisor">
          <i className="ti ti-robot" aria-hidden="true" />
          <span>{f.advisor_note}</span>
        </p>
      ) : null}

      <div className="signal__foot">
        <span style={{ display: "inline-flex", alignItems: "center", gap: "var(--sp-2)" }}>
          <span className="tag">{typeLabel}</span>
          {f.tier === "NEEDS_REVIEW" ? <span className="tag tag--warn">Review</span> : null}
        </span>

        {side === "buy" ? (
          <button
            className="btn btn--sm btn--pos"
            disabled={busy || f.close == null}
            aria-busy={busy || undefined}
            onClick={() => onOpen(f)}
          >
            <i className="ti ti-plus" aria-hidden="true" />
            Open
          </button>
        ) : side === "hold" ? (
          <span className="mut" style={{ display: "inline-flex", alignItems: "center", gap: "var(--sp-1)", fontSize: "var(--fs-micro)" }}>
            <i className="ti ti-circle-check" aria-hidden="true" />
            Holding
          </span>
        ) : held ? (
          <button
            className="btn btn--sm btn--neg"
            disabled={busy}
            aria-busy={busy || undefined}
            onClick={() => onClose(f)}
          >
            <i className="ti ti-logout" aria-hidden="true" />
            Close
          </button>
        ) : (
          <span className="mut" style={{ fontSize: "var(--fs-micro)" }}>flat</span>
        )}
      </div>

      {confirm}
    </article>
  );
}
