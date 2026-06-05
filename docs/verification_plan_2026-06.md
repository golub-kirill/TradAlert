# Verification Plan — Raw-Notes Fixes (2026-06)

Purpose: confirm each change from the raw-notes triage is **(a) correct** (no
regression) and **(b) beneficial** (or, where it's a measurement, that it
produces the honest number), and route the results into the validation program.

Run order: **V0 first** (it gates everything), then V1 (highest value), then
V2 / V4, V3 anytime, V5 to fold into the validation program.

All commands run from the repo root in the project venv.

---

## V0 — Regression gate (applies to every fix)

The standing rule is `pytest tests/` green at the end of every step. This could
**not** be run in the authoring sandbox — its file mount served stale/truncated
copies of just-edited files to the Python importer (writes themselves were fine).
So this is the one verification you must run locally first.

```bash
pytest tests/ -q
```

Success criteria:
- All green. Expected counts: prior 192 **+4** (`test_max_hold_exit.py`) and the
  `test_ticker_health.py` edit (one block-test rewritten to an explicit scale, one
  new default-floor test → net +1).
- If `test_ticker_health.py` shows a failure on a `test_four_consecutive_losses_*`
  name, you're on a stale checkout — `git status` / re-pull; the current file has
  `test_four_losses_block_when_configured` + `test_four_losses_floor_at_quarter_by_default`.

Nothing below should be trusted until V0 is green.

---

## V1 — Max-hold exit (Note 1): the big one

**Correctness (no silent regression):**
1. `pytest tests/test_max_hold_exit.py -q` — time_stop fires at the cap; baseline
   unchanged when off; both modes behave.
2. Bit-identical OFF path: run a baseline **without** `--max-hold-days` and confirm
   it reproduces the pre-change baseline (≈+128R, 46.6% WR). This proves the exit
   is truly OFF by default.
   ```bash
   python -m backtest.run_backtest --no-html --start 2000-01-01
   ```

**Benefit / the honest number:**
```bash
python scripts/compare_max_hold.py --modes hard          # baseline + 15d & 30d hard
python scripts/compare_max_hold.py                       # all four (15/30 x hard/soft)
```
- Success criterion: the **15-bar hard** run is the swing-consistent headline
  (30 bars is lenient vs the ~10–15-day thesis). Adopt that number; expect it to
  be materially below +128R — that's the point.
- Decision (mostly made): `hard` is canonical; confirm 15 vs 30 from the table.
- Lands in: extend ADR-001 "Results" with the 15-bar row; make it the Phase B/C
  headline and retire the uncapped baseline.

---

## V2 — Sweep dead-key fix + breadth dormancy (Note 2)

**Dead-key fix actually moves results:**
```bash
python -m backtest.run_backtest --sweep --quick --no-html --start 2000-01-01
```
- In the output, find the **"Behavioral size floor"** group. Before the fix every
  value gave identical E[R]; after, values 0.25→0.65 should now **vary** (the key
  reaches the live behavioral sizer). Success: non-identical rows for that param.
- If it's *still* identical, the cause is the breadth layer being dormant (below),
  not the key — check that next.

**Breadth-divergence dormancy (the "wired but no effect" question):**
- One-off diagnostic: count how often the breadth-divergence flag is `True` across
  the backtest window (instrument `classify_behavioral_state` / the divergence
  predicate to log or tally events). Success: a concrete `% of bars divergence
  fired`. If ≈0 → confirmed dormant (flat sweep row is *data*, not a bug). If
  non-trivial → the `breadth_divergence_penalty` sweep row should also move.
- Lands in: append the measured divergence frequency to triage Note 2.

---

## V3 — Report coloring (Note 3): cosmetic correctness

```bash
python -m backtest.run_backtest --no-html --start 2020-01-01        # in a real terminal
python -m backtest.run_backtest --no-html --start 2020-01-01 | cat  # piped
```
- Success: in the TTY run, the headline block (Expectancy, Total R, Profit factor,
  Best/Worst) and the by-signal/regime/exit/year E[R] now show green/red by sign,
  matching the attribution table; counts/WR/drawdown stay neutral. In the piped
  run there are **no** ANSI escape codes (logs stay clean — `_USE_COLOR` off).
- Low stakes; no decision rides on this.

---

## V4 — Chronic penalty (Note 4): keep-or-drop decision

```bash
python scripts/ab_chronic_penalty.py                     # OFF vs ON (de-fanged schedule)
```
- Decision rule: **keep** the penalty only if Sharpe and/or Calmar improve **and**
  max-DD drops, for an acceptable Total-R give-up. If Total-R falls with no
  risk-adjusted gain → leave it OFF; the portfolio `max_drawdown_r` breaker is the
  better lever (the per-ticker streak signal is weak — see Note 4 analysis).
- Deeper (later): re-run the conditional-WR + regime-control analysis once
  bear/shorts are enabled — the regime confound is untestable while only bull
  regimes trade.
- Lands in: TODO Note 4 closed with the decision; record numbers in the ADR or a
  short note.

---

## V5 — Anti-overfit tie-in (validation program)

Any fix that changes the headline — V1 above all — must clear the existing
de-biasing gates before it's trusted, so the new number isn't itself an artifact:

```bash
python -m backtest.run_backtest --max-hold-days 15 --walk-forward --no-html --start 2000-01-01
python -m backtest.run_backtest --max-hold-days 15 --robustness  --no-html --start 2000-01-01
```
- Success: the capped-hard edge survives walk-forward OOS and ±10/20% robustness,
  and stays positive with frictions (slippage/commission) on — bootstrap CI
  excluding zero. Then it earns a place as the headline.

---

## Definition of done

- [ ] `pytest tests/` green (V0).
- [ ] OFF-path baselines bit-identical to pre-change (V1.2) — proves opt-in safety.
- [ ] 15-bar `hard` headline measured and adopted in the validation program (V1, V5).
- [ ] "Behavioral size floor" sweep row confirmed live; breadth divergence
      frequency measured (V2).
- [ ] Chronic A/B run and keep/drop decided on Sharpe/Calmar/max-DD (V4).
- [ ] Coloring spot-checked (V3).
- [ ] Results captured in ADR-001 + triage doc; TODO items closed.

## What would invalidate a "benefit" claim

- A capped headline that looks fine in-sample but **fails walk-forward OOS** → the
  edge was in the long-hold tail / overfit, not real.
- Chronic penalty that lifts Total-R but not Sharpe/Calmar → it's adding return by
  taking more risk, not improving the strategy.
- Any "benefit" measured on a single window — prefer the walk-forward view.
