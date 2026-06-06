# ADR-002: Score-based exit — rejected (kept as live advisory only)

**Status:** Rejected (investigated, reverted) — 2026-06-05
**Deciders:** repo owner
**Related:** ADR-001 (max-hold exit); `scoring._score_exit` / `_score_rs_exit`

## Context

The exit-side scoring blend (`_score_exit`: `regime_flip`, `multi_bar_decay`,
`rsi_overbought`, `macd_cross_down`, `vol_expansion`, `rs_divergence`,
`vbp_resistance` → a 0–100 weighted average) drives the **live** exit-alert score in
`main.py`, but never affected the backtest. Backtest exits are mechanical: stop /
target / `time_stop` / `engine_exit`.

The question raised: is `_score_rs_exit` (and the wider exit blend) actually doing
anything, and would wiring it as a backtest exit driver improve the strategy?

## Decision

**Reject.** The exit score stays a live advisory only; it does **not** drive backtest
or strategy exits. The opt-in implementation built to test the idea was reverted.

## Investigation

Built an opt-in, OFF-by-default independent score-exit: a held position closes once
its `_score_exit` reaches `exit_score_min`, scored on the completed bar and filled at
the next open like `engine_exit` (exit reason `score_exit`). Measured against the OFF
baseline (`run_id=8`, 25d-hard: **+87.2R, Sharpe 0.58, 1194 trades**) across thresholds
and three weighting theses.

### Score distribution (held long, BULL regime)

The default blend caps around **47** in BULL, because `regime_flip` (weight 4/18) is 0
while the market is bullish. Thresholds ≥ 50 therefore never fire on held longs (the
first A/B at 50/60/70 was a perfect no-op). Useful thresholds live in ~30–45.
Diagnostic: 4,585+ samples, 0 errors — the blend is real and computed.

### Thesis 1 — default blend (exit on weakness): net-negative

| exit_score_min | Total R | Sharpe | score_exit cohort |
|----------------|---------|--------|-------------------|
| 40 | +77.2 | 0.51 | 73t · WR 18% · E[R] −0.33 |
| 35 | +52.2 | 0.36 | 343t · WR 27% · E[R] −0.19 |
| 30 | +45.8 | 0.33 | 431t · WR 31% · E[R] −0.14 |

The cohort *loses* at every threshold — it cuts weak-looking positions that
mean-revert. The exit signal is anti-correlated with the edge.

### Thesis 2 — `regime_flip` only: no-op (redundant)

Byte-identical to OFF, zero `score_exit` fires. When the market leaves BULL the engine
already fires `engine_exit` on the same bar, so a regime-weighted score-exit only
duplicates engine exits (in BULL it can never fire). Its exits are a strict subset of
the engine's.

### Thesis 3 — take-profit (`rsi_overbought` + `vbp_resistance`): net-negative, cohort profitable

| exit_score_min | Total R | Sharpe | score_exit cohort |
|----------------|---------|--------|-------------------|
| 70 | +78.4 | 0.52 | 65t · WR 82% · E[R] +0.39 |
| 60 | +66.2 | 0.45 | 131t · WR 82% · E[R] +0.26 |
| 50 | +2.1  | 0.02 | 1303t · WR 53% · E[R] +0.08 |

These exits are *individually* profitable (overbought-at-resistance is a real
take-profit signal), but they cap winners that the existing target / `time_stop` exits
ride further (the OFF `time_stop` cohort earns +0.52R). The headline converges toward
OFF from below as the threshold rises — it never exceeds it.

## Trade-off / conclusion

The unifying result: **the strategy's existing exits already capture the available
edge.** A score-based exit can only (a) duplicate the regime exit (`engine_exit`
already does it), (b) fight the mean-reversion edge (weakness exits), or (c) cap
winners early (take-profit). None beats the baseline; there is no fourth bucket.

`_score_rs_exit` is therefore **real, not fake** — it computes a genuine
relative-strength divergence — but mechanically exiting on the blend cannot improve
this strategy.

## Consequences

- **Reverted** the feature (no `PortfolioConfig.exit_score_min`, no `score_exit` exit
  reason, no `SignalScorer.exit_score`, no `--exit-score-min` flag). Baseline is
  unchanged.
- The exit score remains a **live decision aid** in `main.py` for the operator holding
  a position — its advisory value is out of scope here.
- **Possible future use (not now):** the take-profit variant produced a *profitable*
  cohort, so it might help a separate *tighter-target* strategy variant — a distinct
  study, not this one.
