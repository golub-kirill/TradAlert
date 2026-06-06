# ADR-003: Entry scoring made optional, default OFF (non-predictive of R)

**Status:** Accepted — scoring is opt-in via `--scoring` (2026-06-05)
**Deciders:** repo owner
**Related:** ADR-002 (score-based *exit* rejected); `core.scoring.SignalScorer`;
`scanner.min_score_to_alert`; `backtest/sweep.py` (scorer injection)

## Context

The `SignalScorer` produces a 0–100 "confidence" per entry signal and drives **two**
real decisions:
1. The **`min_score_to_alert` gate** — signals below threshold become `watch_only`
   (skipped as entries in the backtest, demoted to watchlist live).
2. The **budget tiebreak** — when many signals compete for the `max_open_risk`
   budget, pending entries fill **highest-score-first** (`portfolio_backtester`).

Two observations exposed it. First, expanding the watchlist to 213 names produced a
universe (Sharpe 0.42) that underperformed *both* its ETF-only (0.39) and stocks-only
(0.56) subsets — combining couldn't be worse than its parts unless selection was
actively mis-picking. Second, with `entry_score` now journaled (it had always been 0),
the relationship to realized R is measurable:

**corr(entry_score, r_multiple) = −0.03 over 1495 trades (run_id=10) — noise.** And
because the budget fills highest-score-first, it preferred the 75+ band (avg +0.030R)
over the 70–75 band (+0.128R) — systematically selecting the *weaker* trades under
competition. (ADR-002 already rejected the *exit* score; this is the entry side.)

## Decision

**Make scoring opt-in, default OFF.** A `--scoring` flag on `run_backtest.py` and
`main.py` (and `SweepEngine(use_scoring=...)`) wires the `SignalScorer`; without it the
scorer is never constructed — entries are taken un-gated and the budget fills
alphabetically (`scorer=None`, already supported). `--scoring-sweep` forces it on (it
tunes the scorer). The scorer is retained, not deleted, so the layer can be
re-calibrated and re-tested.

## Results (213-name universe, 25d if_not_profit @ budget 5.0, slippage 0.002)

| Metric | Scoring ON (run_id=10) | **Scoring OFF (run_id=11)** |
|--------|------------------------|-----------------------------|
| Trades | 1495 | 1560 |
| Win rate | 43.3% | 44.3% |
| E[R] | +0.046 | **+0.075** |
| Total R | +68.9 | **+116.7** |
| Profit factor | 1.17 | **1.30** |
| **Sharpe (monthly)** | 0.42 | **0.66** |
| Sortino | 0.69 | **1.16** |
| E[R] 95% CI | [+0.007, +0.086] | **[+0.036, +0.113]** |

Turning scoring off lifts Sharpe **0.42 → 0.66** and moves the bootstrap CI floor from
near-zero to robustly positive. The ON control reproduces run_id=10 exactly on the new
code, so the delta is the scoring layer, not a code change. This is on the *broad*
universe (less survivorship), so the gain is real, not selection.

## Consequences

- **New headline: run_id=11** — 25d if_not_profit @ budget 5.0, slippage 0.002,
  scoring OFF: **+116.7R, Sharpe 0.66, PF 1.30** over 1560 trades.
- **Live alerts are no longer score-gated** — every engine-triggered fire is actionable
  (fires = momentum/mean-rev triggers, not all scan-passers, so volume rises modestly).
- **Behavioral sizing is unaffected** — it is keyed off `settings`, not the scorer.
- **Chart**: the header badge omits the score when 0 (un-enriched) so it no longer reads
  a misleading "LONG 0".
- **Follow-up:** the scorer isn't deleted. If a future re-calibration makes the score
  predictive (corr > 0), re-enable selectively; until then it is dead weight that hurts.
