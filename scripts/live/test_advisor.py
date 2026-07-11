#!/usr/bin/env python3
"""
Advisor plumbing smoke test — random backtest trade(s) → LLM verdict.

Feeds real journaled trade setups through the full advisor chain (news fetch →
Ollama → formatted note) and prints each verdict next to the realized R, so you
can eyeball the advisor's judgment on real signal shapes. Forces
advisor.enabled for the run regardless of settings.yaml.

⚠ NOT an eval. Ticker news and macro context are fetched TODAY while the trade
is up to a year old — the news axis is anachronistic (post-entry or irrelevant),
so verdict-vs-outcome here is plumbing/judgment color, never evidence. The
honest measurement is the prospective live journal (scan_results.advisor_note
scored against realized R after >=30-50 live verdicts).

Usage:
    python scripts/live/test_advisor.py
    python scripts/live/test_advisor.py --seed 42 --count 5     # reproducible sample
    python scripts/live/test_advisor.py --model qwen3:8b --no-macro
    python scripts/live/test_advisor.py -v                      # show news, prompt, raw verdict
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


# ── helpers ──────────────────────────────────────────────────────────

def _min_rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return reward / risk if risk > 0 else 0.0


def _actual_label(r: float | None) -> str:
    if r is None:
        return "N/A"
    return "PROFIT" if r > 0 else ("FLAT" if r == 0 else "LOSS")


def _fetch_trades(seed: int | None, count: int) -> list[dict]:
    """Random qualifying trades from the last 365 days. [] on DB failure.

    Reproducibility lives in SQL: MySQL's RAND(N) is deterministic for a given
    seed (a Python random.seed() would have no effect on ORDER BY RAND()).
    """
    from persistence.db_conn import connect

    base = """
        SELECT ticker, direction, signal_type,
               entry_date, exit_date,
               entry_price, initial_stop, initial_target,
               r_multiple, effective_r, exit_reason,
               market_regime, ticker_trend
        FROM backtest_trades
        WHERE entry_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
          AND initial_stop IS NOT NULL
          AND initial_target IS NOT NULL
          AND ticker_trend IS NOT NULL AND ticker_trend != ''
          AND market_regime IS NOT NULL
        ORDER BY {order}
        LIMIT %(n)s
    """
    if seed is not None:
        sql = base.format(order="RAND(%(seed)s)")
        params = {"seed": seed, "n": count}
    else:
        sql = base.format(order="RAND()")
        params = {"n": count}

    conn = None
    try:
        conn = connect()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()
    except Exception as exc:
        print(f"  DB query failed ({exc}). Set DB_* in config/secrets.env "
              "and journal a backtest first (python -m backtest.run_backtest).")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── advisor chain (stepwise, so we can show every stage) ─────────────

@dataclass
class _Review:
    """One trade run through the advisor, with the intermediates exposed."""

    note: str = ""
    verdict: object = None          # AdvisorVerdict | None
    headlines: list = field(default_factory=list)
    company_name: str = ""
    prompt: str = ""
    elapsed: float = 0.0
    error: str = ""                 # short reason
    traceback: str = ""             # full trace (verbose only)
    bull: object = None             # BullCase | None (debate mode)
    bear: object = None             # BearCase | None (debate mode)
    fell_back: bool = False         # debate judge failed -> single-shot verdict


def _review_trade(trade: dict, signal, ctx) -> _Review:
    """Mirror of ``advise_signal`` that returns the intermediates instead of
    swallowing them. Surfaces errors rather than collapsing to an empty note.

    Shares ``build_advisor_input`` with the live path so new fields stay in sync.
    Posture fields are None here — backtest_trades carries no last-bar snapshot."""
    from core.advisor.client import ask_llm
    from core.advisor.prompts import build_prompt
    from core.advisor.service import build_advisor_input, format_note

    rv = _Review()
    ticker = trade["ticker"]
    try:
        input_data = build_advisor_input(ticker, signal, ctx)
        rv.company_name = input_data.company_name
        rv.headlines = input_data.headlines
        rv.prompt = build_prompt(ticker, input_data)
        t0 = time.time()
        if getattr(ctx, "debate_enabled", False):
            from core.advisor.debate import run_debate
            dr = run_debate(input_data, ctx)
            rv.verdict, rv.bull, rv.bear, rv.fell_back = (
                dr.verdict, dr.bull, dr.bear, dr.fell_back)
        else:
            rv.verdict = ask_llm(
                input_data,
                endpoint=ctx.endpoint, model=ctx.model, timeout=ctx.timeout,
                temperature=ctx.temperature, max_tokens=ctx.max_tokens,
                session=ctx.session,
            )
        rv.elapsed = time.time() - t0
        rv.note = format_note(rv.verdict) if rv.verdict else ""
    except Exception as exc:  # surfaced, not swallowed — this is a diagnostic tool
        rv.error = f"{type(exc).__name__}: {exc}"
        rv.traceback = traceback.format_exc()
    return rv


def _ollama_status(ctx) -> tuple[bool, list[str], str]:
    """(reachable, model_names, error). A quick GET /api/tags so a down/empty
    Ollama is reported up front instead of as silent empty verdicts."""
    try:
        r = ctx.session.get(f"{ctx.endpoint.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [str(m.get("name", "")) for m in (r.json().get("models") or [])]
        return True, models, ""
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


# ── printing ─────────────────────────────────────────────────────────

def _headline_line(h: dict) -> str:
    title = h.get("headline") or h.get("title") or h.get("summary") or str(h)
    src = h.get("source") or h.get("publisher") or ""
    when = h.get("datetime") or h.get("date") or h.get("published") or ""
    tail = " · ".join(x for x in (str(src), str(when)) if x)
    return f"{title}" + (f"   [{tail}]" if tail else "")


def _print_trade(trade: dict, signal, rv: _Review, *, verbose: bool) -> None:
    entry = float(trade["entry_price"])
    # Raw per-unit R is the outcome the advisor's read maps to; effective_r
    # folds in size_mult/borrow (sizing layer), shown alongside when it differs.
    r_raw = float(trade["r_multiple"]) if trade["r_multiple"] is not None else None
    r_eff = float(trade["effective_r"]) if trade["effective_r"] is not None else None

    result_str = f"{r_raw:+.2f}R" if r_raw is not None else "N/A"
    if r_eff is not None and r_raw is not None and abs(r_eff - r_raw) > 1e-4:
        result_str += f" (effective {r_eff:+.2f}R)"

    print("  " + "─" * 56)
    print(f"  {trade['ticker']:<8} {trade['direction'].upper():<6} "
          f"{trade['signal_type'] or 'setup':<16}  "
          f"{trade['entry_date']} → {trade['exit_date']}")
    print(f"  Entry   {entry:<10.2f} Stop  {signal.stop_price:<10.2f} "
          f"Target {signal.target_price:<10.2f} R:R {signal.min_rr:.1f}")
    print(f"  Regime  {trade['market_regime']:<20}  "
          f"Trend  {trade['ticker_trend']:<10}")
    print(f"  Exit    {trade['exit_reason'] or '—':<20}  "
          f"Result  {result_str}")

    if rv.note:
        print(f"  Advisor  {rv.note}")
    elif rv.error:
        print(f"  Advisor  (failed — {rv.error}; run with -v for the traceback)")
    else:
        print("  Advisor  (no verdict — Ollama unreachable or empty response; -v for detail)")

    v = rv.verdict.verdict.upper() if rv.verdict else "—"
    print(f"  Verdict  {v:<12}  Actual  {_actual_label(r_raw)}")

    if not verbose:
        return

    # ── verbose detail ──────────────────────────────────────────────
    print("  · · · · · · · · · · · · · · · · · · · · · · · · · · · · ·")
    comp = f"{rv.company_name}" if rv.company_name else "(no company_names.json match)"
    print(f"  Company    {comp}")
    if rv.headlines:
        print(f"  Headlines  {len(rv.headlines)} fetched")
        for h in rv.headlines:
            print(f"    • {_headline_line(h)}")
    else:
        print("  Headlines  (none — advisor sees 'no ticker news')")
    if rv.bull is not None or rv.bear is not None:
        print("  Debate ▽")
        if rv.bull is not None:
            print(f"    🐂 Bull   {rv.bull.thesis or '(none)'}")
            for p in rv.bull.points:
                print(f"       · {p}")
        else:
            print("    🐂 Bull   (no case returned)")
        if rv.bear is not None:
            print(f"    🐻 Bear   {rv.bear.thesis or '(none)'}")
            for p in rv.bear.points:
                print(f"       · {p}")
            if rv.bear.rebuttal:
                print(f"       ↩ {rv.bear.rebuttal}")
        else:
            print("    🐻 Bear   (no case returned)")
        if rv.fell_back:
            print("    ⚖ Judge  (failed — fell back to single-shot verdict)")
    if rv.verdict is not None:
        print(f"  LLM        {rv.elapsed:.1f}s   confidence {rv.verdict.confidence:.0%}")
        print(f"  Reasoning  {rv.verdict.reasoning or '(empty)'}")
        print(f"  Risks      {rv.verdict.risks or '(none)'}")
    elif rv.error:
        print(f"  LLM        ERROR — {rv.error}")
        for tline in rv.traceback.rstrip().splitlines():
            print(f"    {tline}")
    else:
        print(f"  LLM        {rv.elapsed:.1f}s — no verdict returned "
              "(unreachable / timeout / empty / malformed JSON; see [W] log lines)")
    print("  Prompt ▽")
    for pline in rv.prompt.splitlines():
        print(f"    {pline}")


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="AI advisor vs backtest trades (plumbing smoke test)")
    ap.add_argument("--seed", type=int, default=None,
                    help="MySQL RAND(seed) for reproducible trade selection")
    ap.add_argument("--count", type=int, default=1,
                    help="number of random trades to review [1]")
    ap.add_argument("--model", default=None,
                    help="override advisor.model for this run (e.g. qwen3:8b)")
    ap.add_argument("--no-macro", action="store_true",
                    help="skip the macro-context summarization (faster)")
    ap.add_argument("--debate", action="store_true",
                    help="run the multi-agent bull/bear/judge critic instead of "
                         "the single-shot verdict (slower; -v shows the transcript)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show company name, news headlines, the full prompt, and "
                         "the raw verdict fields (plus INFO/WARNING advisor logs)")
    args = ap.parse_args()

    # Surface the advisor's own fail-open log lines (news fallbacks, Ollama
    # unreachable/timeout) with a clean prefix so they read as notices, not crashes.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="  [%(levelname).1s] %(name)s: %(message)s",
    )

    import yaml

    from core.advisor import build_advisor_context
    from core.types import SignalResult

    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}

    # Force-enable for the test run — the shipped default is OFF, and a smoke
    # test that silently exercises the disabled path tests nothing.
    settings.setdefault("advisor", {})["enabled"] = True
    if args.model:
        settings["advisor"]["model"] = args.model
    if args.no_macro:
        settings.setdefault("news", {})["macro_summarization"] = False
    if args.debate:
        settings.setdefault("advisor", {}).setdefault("debate", {})["enabled"] = True

    trades = _fetch_trades(args.seed, max(1, args.count))
    if not trades:
        print("  No qualifying backtest trades in the last 365 days "
              "(need stop/target/trend/regime).")
        return

    ctx = build_advisor_context(settings)

    print()
    print(f"  Model      {ctx.model}   @ {ctx.endpoint}")
    print(f"  Macro ctx  {'yes' if ctx.market_context else 'no'}"
          f"   News keys: finnhub={'y' if ctx.finnhub_key else 'n'} "
          f"brave={'y' if ctx.brave_key else 'n'}"
          f"   company_names={len(ctx.company_names)}")

    up, models, err = _ollama_status(ctx)
    if not up:
        print(f"  Ollama     UNREACHABLE at {ctx.endpoint} — every verdict will be "
              f"empty ({err}). Start it: `ollama serve`.")
    elif ctx.model not in models and not any(m.split(":")[0] == ctx.model.split(":")[0]
                                             for m in models):
        print(f"  Ollama     up, but '{ctx.model}' is NOT pulled "
              f"(have: {', '.join(models) or 'none'}). `ollama pull {ctx.model}`.")
    else:
        print(f"  Ollama     reachable ({len(models)} model{'' if len(models) == 1 else 's'})")
    if args.verbose and ctx.market_context:
        print(f"  Macro ▽    {ctx.market_context}")

    for trade in trades:
        entry = float(trade["entry_price"])
        stop = float(trade["initial_stop"])
        target = float(trade["initial_target"])

        signal = SignalResult(
            passed=True,
            direction=trade["direction"],
            signal_type=trade["signal_type"] or "momentum",
            stop_price=stop,
            target_price=target,
            min_rr=_min_rr(entry, stop, target),
            market_regime=trade["market_regime"] or "",
            ticker_trend=trade["ticker_trend"] or "",
            reason=f"{trade['signal_type'] or 'setup'} signal",
        )
        rv = _review_trade(trade, signal, ctx)
        _print_trade(trade, signal, rv, verbose=args.verbose)

    print("  " + "─" * 56)
    print("  ⚠ news/macro are PRESENT-DAY vs a historical entry — plumbing "
          "smoke test only, not an eval of advisor value.")
    print()


def _owns_console() -> bool:
    """True when this process is the sole owner of its console — i.e. it was
    double-clicked from Explorer and the window vanishes the instant it exits.
    False when launched from an existing cmd/PowerShell/pytest (>1 attached)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint * 2)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buf, 2) <= 1
    except Exception:
        return False


if __name__ == "__main__":
    _own = _owns_console()
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("\n  Crashed. If this is a ModuleNotFoundError, run it from the "
              "project venv:\n    .venv\\Scripts\\python.exe "
              "scripts\\live\\test_advisor.py -v")
    finally:
        # A double-clicked window would close before any of the above is read.
        if _own:
            try:
                input("\n  — done. Press Enter to close —")
            except (EOFError, KeyboardInterrupt):
                pass
