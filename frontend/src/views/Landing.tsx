/* The public face. Every figure here is generated sample data on the TEST.*
 * convention — the page describes the machine, never a live account. */

import { EquityCurve } from "../components/EquityCurve";
import { CascadeWords, MaskWords, Reveal } from "../components/Text";
import { useClickSpark, useMagnetic, useReveal, useScrolled, useSpotlight } from "../hooks/useMotion";
import { useHealth } from "../hooks/useHealth";
import { useTheme } from "../hooks/useTheme";
import { Link } from "../lib/router";
import { cssVars } from "../lib/motion";
import { demoCurve, demoHeadline, demoPipeline } from "../api/demo";

const VOCAB = [
  "BULL", "CHOP", "BEAR", "MOMENTUM", "MEAN REVERSION", "TIME STOP", "REGIME EXIT",
  "BREAKEVEN TRIGGER", "OPEN-RISK BUDGET", "WALK-FORWARD", "EXPECTANCY",
  "PROFIT FACTOR", "MAX DRAWDOWN", "STAND-DOWN", "R MULTIPLE",
];

const THROUGHPUT = [
  { value: "412", label: "symbols per scan", hint: "watchlist with fresh bars" },
  { value: String(demoHeadline.months), label: "months replayed", hint: demoHeadline.window },
  { value: "14", label: "checks per signal", hint: "before anything is journaled" },
];

const CAPABILITIES = [
  {
    span: 7 as const,
    icon: "ti-radar",
    title: "One scan, every session, same rules",
    desc: "The watchlist is swept on a schedule. Liquidity, trend, setup, regime and open-risk are applied in a fixed order, and the result is written down whether it fired or not — including why it didn't.",
    foot: "src/core · scan_results journals rejections too",
    preview: true,
  },
  {
    span: 5 as const,
    icon: "ti-file-certificate",
    title: "Figures carry their provenance",
    desc: "Every headline number states which run produced it, over what window, and whether that run measured the strategy that ships today. When it didn't, the panel says so before it shows the number.",
    foot: "run id · window · config match",
  },
  {
    span: 4 as const,
    icon: "ti-flask",
    title: "Replay the engine",
    desc: "Run the same code over a date range and journal the result — baseline, sweep, or walk-forward.",
    foot: "backtest/ · journaled to the same schema",
  },
  {
    span: 4 as const,
    icon: "ti-briefcase",
    title: "Journal, not broker",
    desc: "Positions, stops and partial exits are recorded. Nothing here places an order — there is no trading path in the codebase.",
    foot: "journal-only by construction",
  },
  {
    span: 4 as const,
    icon: "ti-robot",
    title: "An optional second opinion",
    desc: "A rubric computes the verdict; a language model only reads the news around a name. It can decline a signal, never rubber-stamp one.",
    foot: "off by default · live-only",
  },
];

function LiveChip() {
  const health = useHealth();
  const label =
    health === "online" ? "engine online" : health === "connecting" ? "checking…" : "engine offline";
  return (
    <span
      className="livechip"
      title="This chip is wired to the running API — the page is checking for itself."
    >
      <span
        className={"dot" + (health === "online" ? "" : health === "offline" ? " dot--off" : " dot--idle")}
        aria-hidden="true"
      />
      {label}
    </span>
  );
}

function SignalPreview() {
  return (
    <div className="signal signal--buy" style={{ background: "var(--surface-1)" }}>
      <div className="signal__top">
        <div style={{ minWidth: 0 }}>
          <div className="signal__ticker">TEST.5</div>
          <div className="signal__name">Test Industrials Corp</div>
        </div>
        <span className="signal__chip signal__chip--buy">
          <i className="ti ti-arrow-up-right" aria-hidden="true" />
          Buy
        </span>
      </div>
      <div className="signal__stats">
        {[
          ["Close", "63.40"],
          ["Stop", "58.90"],
          ["Target", "76.60"],
          ["R:R", "2.93"],
        ].map(([k, v]) => (
          <div key={k}>
            <div className="signal__k">{k}</div>
            <div className="signal__v">{v}</div>
          </div>
        ))}
      </div>
      <p className="signal__reason">trend 3/3 · volume 1.8× · RS 0.91 · above weekly SMA10</p>
    </div>
  );
}

function PipelineStage({
  stage,
  index,
  widest,
}: {
  stage: (typeof demoPipeline)[number];
  index: number;
  widest: number;
}) {
  return (
    <li className="pipe__stage" style={cssVars({ "--i": index })}>
      <span className="eyebrow">Stage {index + 1}</span>
      <span className="pipe__num num">{stage.value.toLocaleString()}</span>
      {/* Square-root scale, not linear: the last three stages are 8%, 2% and
          0.7% of the universe, so a linear bar floors them all at the minimum
          and the funnel stops reading as a funnel. */}
      <span
        className="pipe__bar"
        style={cssVars({
          "--w": `${Math.max(7, Math.sqrt(stage.value / widest) * 100)}%`,
          "--i": index,
        })}
      />
      <span className="pipe__name">{stage.name}</span>
      <span className="pipe__desc">{stage.desc}</span>
    </li>
  );
}

export function Landing() {
  const stuck = useScrolled();
  const { theme, toggle } = useTheme();
  const ctaRef = useMagnetic<HTMLAnchorElement>();
  const spark = useClickSpark();
  const pipeRef = useReveal<HTMLUListElement>();
  const widest = Math.max(...demoPipeline.map((s) => s.value));

  return (
    <div className="lp">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className={"lp__nav" + (stuck ? " stuck" : "")}>
        <div className="lp__wrap lp__navinner">
          <Link className="lp__brand" to="/">
            <span className="dot" aria-hidden="true" />
            TradAlert
          </Link>
          <nav className="lp__navact" aria-label="Primary">
            <button
              className="btn btn--icon"
              onClick={toggle}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              <i className={"ti " + (theme === "dark" ? "ti-sun" : "ti-moon")} aria-hidden="true" />
            </button>
            <Link className="btn" to="/app">
              Open the panel
            </Link>
          </nav>
        </div>
      </header>

      <main id="main">
        {/* ── impact 1: the hero. The equity curve IS the graphic. ────────── */}
        <section className="hero">
          <div className="atmos" aria-hidden="true">
            <div className="atmos__glow" />
            <div className="atmos__grain" />
          </div>
          <div className="hero__curve" aria-hidden="true">
            <EquityCurve points={demoCurve} variant="hero" strokeWidth={2} />
          </div>

          <div className="lp__wrap hero__inner">
            <p className="hero__eyebrow eyebrow">
              <span className="dot" aria-hidden="true" />
              Systematic · rules-based · journal-first
            </p>

            <h1 className="hero__title">
              <MaskWords text="Every signal arrives with its receipts." accent="receipts." />
            </h1>

            <p className="hero__sub">
              TradAlert sweeps a watchlist every session, applies the same rules in the same order
              every time, and writes down what it found — the stop, the target, the reason, and the
              market state it fired in. Including the ones it rejected.
            </p>

            <div className="hero__cta">
              <Link
                className="btn btn--primary btn--lg magnetic"
                to="/app"
                ref={ctaRef}
                onClick={spark}
              >
                Open the control panel
                <i className="ti ti-arrow-right" aria-hidden="true" />
              </Link>
              <a className="btn btn--lg" href="#pipeline">
                See how it decides
              </a>
            </div>

            <div className="hero__figures">
              {THROUGHPUT.map((f) => (
                <div className="hero__fig" key={f.label}>
                  <div className="hero__fignum num">{f.value}</div>
                  <div className="hero__figlabel eyebrow">{f.label}</div>
                  <div className="banner__note" style={{ fontSize: "var(--fs-micro)" }}>
                    {f.hint}
                  </div>
                </div>
              ))}
            </div>

            <p className="mut" style={{ fontSize: "var(--fs-micro)", marginTop: "var(--sp-5)" }}>
              Curve above: generated sample data over {demoHeadline.months} months. Not a live
              account, and not an offer of anything.
            </p>
          </div>
        </section>

        {/* ── impact 2: the band, then the funnel that says no ────────────── */}
        <div className="band" aria-hidden="true">
          <div className="marquee">
            <div className="marquee__track">
              {[0, 1].map((copy) => (
                <div key={copy} style={{ display: "flex" }}>
                  {VOCAB.map((v) => (
                    <span className="band__item" key={copy + v}>
                      <i className="ti ti-point-filled" />
                      {v}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </div>

        <section className="sec" id="pipeline">
          <div className="lp__wrap">
            <Reveal className="sec__head">
              <p className="eyebrow">How it decides</p>
              <h2 className="sec__title">It says no 409 times out of 412.</h2>
              <CascadeWords
                className="sec__lede"
                text="A signal engine is mostly a rejection engine. Each stage below throws work away, and every rejection is journaled with the reason it failed — so a quiet week is a readable result, not an empty screen."
              />
            </Reveal>

            <ul className="pipe stagger" ref={pipeRef} style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {demoPipeline.map((s, i) => (
                <PipelineStage key={s.name} stage={s} index={i} widest={widest} />
              ))}
            </ul>
          </div>
        </section>

        {/* ── impact 3: the capability bento, unequal by design ───────────── */}
        <section className="sec" id="what">
          <div className="lp__wrap">
            <Reveal className="sec__head">
              <p className="eyebrow">What it is</p>
              <h2 className="sec__title">A scanner, a backtester and a journal that agree with each other.</h2>
              <p className="sec__lede">
                The same engine produces the live scan and the historical replay. When they disagree,
                that's a bug — not a footnote.
              </p>
            </Reveal>

            <div className="bento">
              {CAPABILITIES.map((c) => (
                <Capability key={c.title} {...c} />
              ))}
            </div>
          </div>
        </section>

        {/* ── the honesty section: the product's actual differentiator ────── */}
        <section className="sec" id="provenance">
          <div className="lp__wrap">
            <div className="bento">
              <Reveal className="span-7">
                <p className="eyebrow">Why the receipts matter</p>
                <h2 className="sec__title" style={{ fontSize: "var(--fs-display-m)" }}>
                  A dashboard that headlines the wrong run is worse than no dashboard.
                </h2>
                <p className="sec__lede">
                  Backtest journals fill up with experiment arms. Show the newest one and you put an
                  A/B leg's numbers on screen as if they described the strategy. TradAlert picks the
                  newest run that actually measured the shipped configuration, and tells you when it
                  had to step over something to find it.
                </p>
              </Reveal>

              <Reveal className="span-5">
                <div className="banner banner--warn" role="presentation">
                  <i className="ti ti-flask banner__icon" aria-hidden="true" />
                  <div className="banner__body">
                    <strong>Showing run #40 — the newest baseline, not the newest run.</strong>
                    <div className="banner__note">
                      Run #41 ({(demoHeadline.totalR * 0.52).toFixed(2)}R) ran a different config
                      from the shipped <code>filters.yaml</code>, so it measured a different strategy
                      — an A/B leg, not the edge.
                    </div>
                  </div>
                </div>
                <p className="mut" style={{ fontSize: "var(--fs-micro)", marginTop: "var(--sp-3)" }}>
                  A real warning from the panel, reproduced verbatim.
                </p>
              </Reveal>
            </div>
          </div>
        </section>
      </main>

      <footer className="lp__foot">
        <div className="lp__wrap">
          <div className="lp__footrow">
            <Link className="lp__brand" to="/">
              <span className="dot" aria-hidden="true" />
              TradAlert
            </Link>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--sp-3)" }}>
              <LiveChip />
              <Link className="btn btn--primary" to="/app">
                Open the panel
              </Link>
            </div>
          </div>
          <p className="lp__footnote">
            Journal-only research tooling. Nothing on this page is investment advice, an offer, or a
            performance claim; all figures shown are generated sample data on the{" "}
            <code>TEST.*</code> convention.
          </p>
        </div>
      </footer>
    </div>
  );
}

function Capability({
  span,
  icon,
  title,
  desc,
  foot,
  preview,
}: {
  span: 4 | 5 | 7;
  icon: string;
  title: string;
  desc: string;
  foot: string;
  preview?: boolean;
}) {
  const ref = useSpotlight<HTMLDivElement>();
  return (
    <article className={`cap card--spot span-${span}`} ref={ref}>
      <i className={"ti " + icon + " cap__icon"} aria-hidden="true" />
      <h3 className="cap__title">{title}</h3>
      <p className="cap__desc">{desc}</p>
      {preview ? (
        <div className="cap__preview">
          <SignalPreview />
        </div>
      ) : null}
      <p className="cap__foot">{foot}</p>
    </article>
  );
}
