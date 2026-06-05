# Verification Runbook — 2026-06 fixes (hard mode, horizons ≤ 30)

Decision locked: max-hold = **hard** mode, horizons tested up to **30** bars.

Run from the repo root in the venv (PowerShell). Every command tees its console
to `logs\` and every ledger goes to `data\backtest_out\<dir>\` — both live inside
the repo, so once a step finishes you can just say **"step N done"** and point me
at the folder; I'll read the logs + CSVs directly. (Pasting the noted block also
works.) Fastest signal: do **Steps 1–3 first**.

```powershell
# Step 0 — once per PowerShell session:
mkdir logs -Force
$env:PYTHONUTF8 = "1"                                   # UTF-8 so piped output doesn't crash
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # clean (non-mojibake) log files
```
> The entry points were also hardened to force UTF-8 stdout, so a missed
> `$env:PYTHONUTF8` no longer crashes — but setting both lines gives the cleanest
> log files on Windows (cp1252) consoles.

---

## Step 1 — pytest (code correctness)

```powershell
pytest tests\ -q 2>&1 | Tee-Object logs\01_pytest_all.txt
pytest tests\test_max_hold_exit.py tests\test_ticker_health.py -v 2>&1 | Tee-Object logs\01_pytest_new.txt
```
**Artifacts:** `logs\01_pytest_all.txt`, `logs\01_pytest_new.txt`
**I verify:** all green; the 4 `test_max_hold_exit` cases pass; `test_ticker_health`
shows `test_four_losses_block_when_configured` **and**
`test_four_losses_floor_at_quarter_by_default` passing, and **no**
`test_four_consecutive_losses_block` (old name) remains.

---

## Step 2 — OFF-path baseline (proves the exit is truly opt-in)

```powershell
python -m backtest.run_backtest --no-html --start 2000-01-01 --out data\backtest_out\off 2>&1 | Tee-Object logs\02_off.txt
```
**Artifacts:** `logs\02_off.txt`, `data\backtest_out\off\trades.csv`
**I verify:** Total R ≈ **+128**, WR ≈ **46.6%** (matches the pre-change baseline),
and the **By exit** block has **no `time_stop` row** (only engine_exit / stop /
target / open_eod) → confirms OFF changes nothing.

---

## Step 3 — hard, 30-bar cap (the canonical run, full ledger)

```powershell
python -m backtest.run_backtest --max-hold-days 30 --max-hold-mode hard --no-html --start 2000-01-01 --out data\backtest_out\h30 2>&1 | Tee-Object logs\03_h30.txt
```
**Artifacts:** `logs\03_h30.txt`, `data\backtest_out\h30\trades.csv`
**I verify (from the CSV directly):** a `time_stop` exit reason exists; every
`time_stop` and `engine_exit` trade has `bars_held ≤ 30` (stop/target may exceed —
they trigger first); I recompute WR / Total R from the CSV and confirm they match
the console; and the startup line printed `▸ Max-hold exit: ENABLED (30 bars,
mode=hard …)`.

---

## Step 4 — hard across horizons 10 → 30 (the headline curve)

```powershell
python scripts\compare_max_hold.py --days 10 15 20 25 30 --modes hard 2>&1 | Tee-Object logs\04_compare_hard.txt
```
*(~18 min: 1 baseline + 5 hard walks.)*
**Artifact:** `logs\04_compare_hard.txt`
**I verify:** the comparison table (Total R / ΔR / Sharpe / Calmar / maxDD /
avg-held by horizon) and the per-horizon `time_stop` cohort line; then we pick the
headline horizon (≤ 30) together and lock it into the validation program.

---

## Step 5 — Note 2 dead-key (sweep row now moves)

```powershell
python -m backtest.run_backtest --sweep --quick --no-html --start 2000-01-01 --out data\backtest_out\sweep 2>&1 | Tee-Object logs\05_sweep.txt
```
**Artifact:** `logs\05_sweep.txt`
**I verify:** the **"Behavioral size floor"** group rows now show *varying*
E[R]/Total R across values 0.25→0.65 (before the fix they were identical). If
still flat, the breadth layer is dormant (expected) — we measure divergence
frequency separately, not a bug.

---

## Step 6 — Note 3 coloring (cosmetic)

```powershell
# (a) eyeball this in the terminal — look for green/red:
python -m backtest.run_backtest --no-html --start 2020-01-01 --out data\backtest_out\color
# (b) piped copy so I can confirm no stray escape codes:
python -m backtest.run_backtest --no-html --start 2020-01-01 --out data\backtest_out\color 2>&1 | Tee-Object logs\06_color_piped.txt
```
**Artifact:** `logs\06_color_piped.txt` (+ your eyeball)
**I verify:** in your terminal the headline R figures and breakdown E[R] are
green/red by sign (matching the attribution table); in the piped file there are
**no** `\x1b[` ANSI codes (logs stay clean).

---

## Step 7 — Note 4 chronic A/B (keep-or-drop)

```powershell
python scripts\ab_chronic_penalty.py 2>&1 | Tee-Object logs\07_ab_chronic.txt
```
*(~6 min.)*
**Artifact:** `logs\07_ab_chronic.txt`
**I verify:** OFF vs ON table, then apply the rule — keep the penalty only if
Sharpe/Calmar rise **and** max-DD falls for an acceptable Total-R give-up;
otherwise leave it OFF and lean on the portfolio drawdown breaker.

---

## What to send / how I check

| Step | Verifies | Read these |
|------|----------|------------|
| 1 | code correct, no regression | `logs\01_pytest_*.txt` |
| 2 | max-hold truly OFF by default | `logs\02_off.txt`, `off\trades.csv` |
| 3 | time_stop fires & caps correctly | `logs\03_h30.txt`, `h30\trades.csv` |
| 4 | headline curve (hard ≤30) | `logs\04_compare_hard.txt` |
| 5 | sweep dead-key fixed | `logs\05_sweep.txt` |
| 6 | coloring consistent / clean logs | `logs\06_color_piped.txt` |
| 7 | chronic penalty keep/drop | `logs\07_ab_chronic.txt` |

The two **trades.csv** files (Steps 2 and 3) are the strongest checks — with them
I can independently recompute every stat and confirm the time_stop behaviour, so
prioritise those if you only do a couple.

---

# V5 — out-of-sample gate for the headline (25-bar, hard)

Headline locked: **`--max-hold-days 25 --max-hold-mode hard`**. V5 confirms the
(shrunken) edge isn't itself overfit before it becomes the validation headline.
Re-run Step 0's two encoding lines if you opened a new session.

```powershell
$env:PYTHONUTF8 = "1"; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

### V5a — 25-bar hard canonical run (ledger + report) — ~3 min

```powershell
python -m backtest.run_backtest --max-hold-days 25 --max-hold-mode hard --no-html --start 2000-01-01 --out data\backtest_out\h25 2>&1 | Tee-Object logs\10_h25.txt
```
I verify: `h25\trades.csv` caps every exit at 25 bars, `time_stop` fires at 25,
and the console headline matches the curve (~+83R effective / Sharpe ~0.48).

### V5b — walk-forward OOS (THE gate)

Fast path — fixed-config temporal stability (~18 runs, a few minutes). This is the
right test for "does the shipped 25d-hard config survive OOS":
```powershell
python -m backtest.run_backtest --max-hold-days 25 --max-hold-mode hard --walk-forward --wf-no-retune --no-html --start 2000-01-01 --out data\backtest_out\h25_wf 2>&1 | Tee-Object logs\11_h25_walkforward.txt
```
Optional, heavier — re-tune sweep per window (tests whether *parameter selection*
generalises; now parallel after the perf fix, so pass `--workers`):
```powershell
python -m backtest.run_backtest --max-hold-days 25 --max-hold-mode hard --walk-forward --workers 14 --no-html --start 2000-01-01 --out data\backtest_out\h25_wf_retune 2>&1 | Tee-Object logs\11b_h25_wf_retune.txt
```
I verify: the per-window IS/OOS table — **gate = OOS stays positive** (the edge
holds on data it wasn't tuned on), not just in-sample.

### V5c — robustness (±10/20% param perturbation) under 25d hard — long

```powershell
python -m backtest.run_backtest --max-hold-days 25 --max-hold-mode hard --robustness --no-html --start 2000-01-01 --out data\backtest_out\h25_rob 2>&1 | Tee-Object logs\12_h25_robustness.txt
```
I verify: **gate = no parameter's ±perturbation collapses E[R] > 50%** (the
"FLAGGED" list is empty/small) — i.e. the edge doesn't hinge on a knife-edge knob.

### Complete Step 5 — dead-key sweep (was interrupted) — long

```powershell
python -m backtest.run_backtest --sweep --quick --no-html --start 2000-01-01 --out data\backtest_out\sweep 2>&1 | Tee-Object logs\05_sweep.txt
```
I verify: the **"Behavioral size floor (group: phase8)"** results table now shows
*varying* E[R]/Total R across 0.25 / 0.50 / 0.65 (dead-key fix confirmed live).

### Optional — Step 6 coloring piped log

```powershell
python -m backtest.run_backtest --no-html --start 2020-01-01 --out data\backtest_out\color 2>&1 | Tee-Object logs\06_color_piped.txt
```

| Step | Verifies | Read these |
|------|----------|------------|
| V5a | 25d cap + headline | `logs\10_h25.txt`, `h25\trades.csv` |
| V5b | edge survives OOS | `logs\11_h25_walkforward.txt` |
| V5c | edge robust to perturbation | `logs\12_h25_robustness.txt` |
| 5   | dead-key sweep row moves | `logs\05_sweep.txt` |
| 6   | clean logs / colors | `logs\06_color_piped.txt` |

Order: **V5a** (fast, confirms headline) → **V5b** (the real OOS gate; leave it
running) → **V5c** → finish Step 5. V5b is the one that decides whether the
25-bar-hard edge earns the validation headline.
