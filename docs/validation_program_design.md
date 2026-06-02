# Validation & De-biasing Program — System Design

Status: design. Owner: TradAlert. Created 2026-05-30.

## Why this exists (the honest framing)

The backtester is well-engineered (T+1 fills, pessimistic stops, no-look-ahead
test, bootstrap CIs, MC drawdown). The open question is **not** "is the code
good" — it is "is the +143R / Sharpe-0.62 edge *real*, or an artifact of how
the universe and parameters were chosen." Edge is a **measurement** problem,
not an engineering problem. The deliverables below mostly try to *reduce* the
headline number until only the trustworthy part remains. A strategy that
survives is believable; one that needs the biases was never real.

Acceptance for the whole program: a number we'd stake money on is one that is
(a) survivorship/selection-debiased, (b) net of realistic costs, (c) measured
out-of-sample, (d) corrected for the many configs tried, and (e) reconciled to
live paper fills. Until then, treat all R figures as upper bounds.

## Requirements

Functional

- Quantify the survivorship/selection discount on the tier_a (hand-picked)
  universe without buying data.
- Resolve "dead ticker" fetch failures that are really symbology mismatches
  (`ABC.DE.TO` → `ABC-DE.TO`, share-class `BRK.B` → `BRK-B`).
- Run the same strategy on a *frozen-as-of-date* universe and diff vs the
  full hindsight list.
- Keep every experiment reproducible and opt-in (baseline replays identically
  when the new paths are off).

Non-functional

- Free data only for v1 (no paid feed).
- No change to baseline numbers unless a debias flag is set.
- Must run inside the existing loader/backtester; no architecture rewrite.

Constraints (from the operator)

- Focus universe: **tier_a hand-picked** (selection bias, not index membership).
- Delisted single-stock prices are out of scope for v1.
- Some fetch failures are symbology, not death: `.`→`-` normalization needed.

## Phased plan (sequenced; each gate must pass before the next earns effort)

| Phase | Deliverable                                                    | Question it answers                             |
|-------|----------------------------------------------------------------|-------------------------------------------------|
| **A** | Survivorship/selection audit (this doc's focus)                | How much of +143R is hindsight?                 |
| B     | Cost/slippage/borrow ON by default                             | Does the edge survive frictions?                |
| C     | Locked out-of-sample protocol (train ≤2015, test once 2016-26) | Does it hold on unseen data?                    |
| D     | Multiple-testing correction (deflated Sharpe / reality check)  | Is the "best" config just luck from many tries? |
| E     | Paper-trade + live-vs-backtest reconciliation journal          | Does it work with real fills?                   |

Phases B-E are scoped later. Phase A is detailed below.

## Phase A — deep dive

### A0. Ticker symbology normalizer (quick win, unblocks the rest)

Problem: `yf_fetchOne.py` passes the raw symbol to `yf.Ticker()`. yfinance uses
`-` for share-class / compound symbols. So `ABC.DE.TO` (operator's example),
`BRK.B`, `BF.B` etc. silently fail and masquerade as "delisted," inflating the
apparent dead-ticker count and quietly shrinking the universe.

Design: a pure function `to_yf_symbol(ticker) -> str` in a new
`src/core/fetchers/symbology.py`:

- Split off a known exchange suffix (`.TO`, `.V`, `.L`, …) — keep it verbatim.
- In the *base* symbol, replace interior `.` with `-` (yfinance convention).
- Re-attach the suffix. `ABC.DE.TO` → base `ABC.DE` + suffix `.TO` →
  `ABC-DE.TO`. `BRK.B` (no suffix) → `BRK-B`. `RY.TO` → unchanged.
- Idempotent; identity for already-clean symbols.
  Wire it at the single yfinance call site in `yf_fetchOne.py`. Pure + unit
  tested (table of cases). No network. Baseline-safe (clean symbols unchanged).

### A1. Inception-aware frozen-universe A/B

Insight: for a hand-picked ETF list there is no "membership" to reconstruct.
The two real biases are:

1. **Look-ahead inclusion** — ICLN (2008), KWEB (2013), ARKK (2014), SOXX
   (2001) etc. cannot be traded before they existed; including them in a
   2001-start run is hindsight on *which themes won*.
2. **Selection/prune bias** — the list was curated and chronic losers were
   removed *after seeing* them lose (documented in the watchlist comments).

Control experiment (no external data):

- **Frozen list**: a config block naming the universe "as of" a past date
  `D_freeze` (e.g., 2010-01-01) — only tickers a neutral observer would have
  picked then (broad indices, sectors, large liquid ETFs), explicitly
  excluding anything launched after `D_freeze` and anything added to the list
  *because* of backtest results.
- Run baseline on (i) full hindsight list and (ii) frozen list over the same
  window; the **delta in total R / Sharpe / DD is the selection discount.**
- Also report, per ticker, `first_bar_date` vs `D_freeze` so look-ahead
  inclusions are visible.

Mechanics: reuse `--tickers` (already exists) — the frozen list is just a
documented subset fed via a YAML key `watchlist.frozen_asof` + a
`--frozen-universe` flag (or `--tickers @config/frozen_2010.txt`). No engine
change; this is a universe-selection harness + a diff report.

### Data model / config

```yaml
# config/watchlist.yaml (new optional block)
frozen_universe:
  as_of: 2010-01-01
  tickers: [SPY, QQQ, DIA, IWM, IWB, MDY, IJR, VTI, XLK, XLF, ...]
  # only names that existed AND were obvious choices at as_of;
  # NO post-as_of launches, NO backtest-pruned survivors re-added.
```

### Data flow

```
watchlist.yaml ──> loader ──┬─ full hindsight universe ──> backtest ──> ledger_full
                            └─ frozen_universe subset  ──> backtest ──> ledger_frozen
ledger_full + ledger_frozen ──> survivorship_discount report (Δ total R, Sharpe, DD)
```

### Acceptance (Phase A)

- `to_yf_symbol` unit tests pass; re-running the fetch resolves previously
  "dead" compound/share-class tickers (operator confirms in PyCharm).
- A single command prints the **selection discount**: full vs frozen total R,
  Sharpe, max DD, and the count of look-ahead inclusions (first_bar > as_of).
- If the discount is large (say > 30-40% of total R), Phase A's verdict is
  "the headline was substantially hindsight" and we proceed to B/C with
  sober expectations. If small, the edge is more credible.

## Data-source recommendation (researched)

- **tier_a (now):** none. No membership feed exists for a curated ETF list;
  inception-aware frozen A/B is the correct, free de-biasing.
- **tier_b later (optional):** a free date-stamped S&P 500 constituents CSV
  from a public dataset (e.g. the widely-used `fja05680/sp500` GitHub repo),
  cross-checked against the Wikipedia "selected changes" table already scraped
  in `sp500_constituents.py`. Verify license before use.
- **Paid (Norgate / Sharadar SEP/SFP):** gold-standard incl. delisted prices;
  only worth it if the universe expands to full single-stock survivorship-free
  backtests. Not needed for the ETF-centric tier_a.

## Trade-offs

- Frozen A/B *approximates* de-biasing; it can't fully remove selection bias on
  a hand-picked list (only a mechanical index + delisted prices could). It is,
  however, free, fast, and high-information — the right first cut.
- Skipping delisted single-stock prices means Phase A measures the
  selection/look-ahead slice, not the full delisting slice. Documented residual.
- `to_yf_symbol` is heuristic (suffix table + interior-dot rule); rare exotic
  symbols may need explicit overrides — keep a small override map.

## What I'd revisit as it grows

- If tier_b single-stock universes become primary → buy Norgate/Sharadar and
  build a true point-in-time membership + delisted-price loader (Phase A→A2).
- Promote the frozen-universe diff into the standard report once trusted.
- Fold the symbology override map into config if exotic tickers accumulate.
