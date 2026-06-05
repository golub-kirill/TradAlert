"""
Per-ticker chronic-loser tracker.

Purpose
───────
After 2+ consecutive losses on the same symbol within a rolling window,
apply a size penalty (or full block) to new entries on that symbol.
Targets emergent losers that appear after the watchlist has been pruned.

Design contract
───────────────
- Stateful, in-memory ledger of closed trades, indexed by ticker.
- ``record_trade`` is called when a trade closes; ``size_multiplier`` is
  called before a new trade opens — never the other way round, so the
  ledger contains only past trades from the caller's perspective.
- The penalty is **per-ticker**, not portfolio-wide: a hot ticker can
  trade full-size while a cold one is throttled.
- Streak walks back from the most recent trade and stops at the first
  win **or** at a trade older than ``lookback_days``. Stale losses age
  off automatically; the policy is self-healing through time.

Backtest use
────────────
Each ``BarReplayBacktester.run()`` call instantiates one ``TickerHealth``
shared across the bar walk. Trades record on close, lookups happen at
the bar before a new entry opens. No cross-worker state needed because
the policy is per-ticker.

Live scan use
─────────────
``TickerHealth.from_csv(path)`` reads the production ``trades.csv``
once at scanner startup; the same ``size_multiplier(ticker, today)``
call yields the live penalty.

Sliding-scale interpretation
────────────────────────────
The default ``scale = {2: 0.5, 3: 0.25}`` (no hard block) means:
    streak == 0 or 1   → 1.0 (no penalty)
    streak == 2        → 0.5
    streak >= 3        → 0.25 (floor)

For any streak ``k``, the multiplier is ``scale[max(j for j in scale if j <= k)]``,
defaulting to 1.0 when no key fits. A ``4: 0.0`` entry would block entirely and
remains configurable — it was removed from the default on 2026-06-03 because
forward expectancy stays positive even after 4+ consecutive losses (see
docs/triage_raw_notes_2026-06.md, Note 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

DEFAULT_SCALE: Mapping[int, float] = {2: 0.5, 3: 0.25}  # floors at 0.25; no hard block (see docstring)
DEFAULT_LOOKBACK_DAYS: int = 90


@dataclass
class TickerHealth:
    """
    Rolling per-ticker streak tracker with sliding-scale size penalty.

    Parameters
    ----------
    lookback_days : int
        Calendar-day window. Losses older than this are ignored when
        computing the current streak.
    scale : Mapping[int, float]
        ``{consecutive_losses_threshold: size_multiplier}``. Keys must be
        positive integers. See module docstring for interpretation.
    enabled : bool
        Master switch. When False, ``size_multiplier`` always returns 1.0
        and ``record_trade`` is a no-op for lookups. Useful for keeping
        legacy backtest baselines stable.
    """
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    scale: Mapping[int, float] = field(default_factory=lambda: dict(DEFAULT_SCALE))
    enabled: bool = True

    # ticker → list of (exit_date, r_multiple), append-only, ordered by
    # call sequence (assumed chronological per ticker in normal use).
    _ledger: dict[str, list[tuple[date, float]]] = field(default_factory=dict)

    # ─── public API ──────────────────────────────────────────────────────

    def record_trade(self, ticker: str, exit_date: date, r_multiple: float) -> None:
        """Append a closed trade to the ledger.

        Caller must invoke this exactly once per closed trade, at or
        after the trade's actual close. Calls are append-only; later
        calls do not overwrite earlier ones even for the same ticker.
        """
        if not ticker:
            return
        self._ledger.setdefault(ticker, []).append((exit_date, float(r_multiple)))

    def consecutive_losses(self, ticker: str, as_of_date: date) -> int:
        """Count consecutive losses on ``ticker`` looking back from ``as_of_date``.

        A "loss" is ``r_multiple < 0``. The walk:
          1. Starts at the most recent trade on the ticker with exit_date
             ``<= as_of_date``.
          2. Skips trades with ``exit_date < as_of_date - lookback_days``.
          3. Stops at the first win (``r_multiple >= 0``).

        Trades with ``r_multiple == 0`` are treated as non-losses so a
        scratch trade naturally resets the streak.
        """
        trades = self._ledger.get(ticker)
        if not trades:
            return 0

        cutoff = as_of_date - timedelta(days=self.lookback_days)
        streak = 0
        for exit_date, r in reversed(trades):
            if exit_date > as_of_date:
                # Future trade — should not occur in backtest; skip
                # defensively rather than raise.
                continue
            if exit_date < cutoff:
                # Too old; older trades cannot extend the streak.
                break
            if r >= 0:
                break
            streak += 1
        return streak

    def size_multiplier(self, ticker: str, as_of_date: date) -> float:
        """Return the size multiplier ∈ [0.0, 1.0] for a fresh entry.

        ``1.0`` means no penalty. ``0.0`` means block entirely (caller
        should treat ``<= 0`` as do-not-trade, mirroring the existing
        regime ``size_mult`` contract — see ``backtester.py:277``).
        """
        if not self.enabled:
            return 1.0
        streak = self.consecutive_losses(ticker, as_of_date)
        if streak <= 0:
            return 1.0
        # Largest scale key ≤ streak. Sorted once per call — scale is tiny.
        applicable = [k for k in self.scale if k <= streak]
        if not applicable:
            return 1.0
        return float(self.scale[max(applicable)])

    def is_blocked(self, ticker: str, as_of_date: date) -> bool:
        """Convenience: True iff the multiplier is non-positive."""
        return self.size_multiplier(ticker, as_of_date) <= 0.0

    # ─── construction helpers ────────────────────────────────────────────

    @classmethod
    def from_csv(
            cls,
            path: str | Path,
            *,
            lookback_days: int = DEFAULT_LOOKBACK_DAYS,
            scale: Mapping[int, float] | None = None,
            enabled: bool = True,
    ) -> "TickerHealth":
        """Build a ``TickerHealth`` pre-populated from a ``trades.csv``.

        Expects columns ``ticker``, ``exit_date`` (ISO ``YYYY-MM-DD``),
        and ``r_multiple``. Missing or malformed rows are logged and
        skipped — they do not raise.
        """
        import csv

        h = cls(
            lookback_days=lookback_days,
            scale=dict(scale) if scale is not None else dict(DEFAULT_SCALE),
            enabled=enabled,
        )
        p = Path(path)
        if not p.exists():
            logger.info("TickerHealth.from_csv: %s not found, returning empty ledger", p)
            return h

        with p.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            ok, bad = 0, 0
            for row in reader:
                try:
                    t = (row.get("ticker") or "").strip()
                    if not t:
                        bad += 1
                        continue
                    ed = date.fromisoformat((row["exit_date"] or "").strip())
                    r = float(row["r_multiple"])
                except (KeyError, ValueError, TypeError):
                    bad += 1
                    continue
                h.record_trade(t, ed, r)
                ok += 1
        logger.info("TickerHealth.from_csv: loaded %d trades (%d skipped) from %s",
                    ok, bad, p)
        return h

    @classmethod
    def from_config(
            cls, cfg: Mapping | None, *, fallback_enabled: bool = False
    ) -> "TickerHealth":
        """Build from a ``filters.yaml`` ``chronic_loser_penalty:`` block.

        Block schema::

            chronic_loser_penalty:
              enabled: true
              lookback_days: 90
              scale:
                2: 0.5
                3: 0.25          # 4+ floors here; add `4: 0.0` to block instead

        ``fallback_enabled`` is used when the block is missing entirely
        — backtests default to ``False`` (baseline replay stability),
        live scans may pass ``True``.
        """
        if not cfg:
            return cls(enabled=fallback_enabled)
        scale_raw = cfg.get("scale") or DEFAULT_SCALE
        # YAML may parse keys as strings — coerce to int defensively.
        scale = {int(k): float(v) for k, v in scale_raw.items()}
        return cls(
            lookback_days=int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS)),
            scale=scale,
            enabled=bool(cfg.get("enabled", fallback_enabled)),
        )

    # ─── introspection ───────────────────────────────────────────────────

    def snapshot(self, as_of_date: date) -> dict[str, dict]:
        """Return a per-ticker debug snapshot ``{ticker: {streak, mult}}``.

        Useful for logging the current penalty state at the end of a
        backtest run, or for the live scanner's status report.
        """
        out: dict[str, dict] = {}
        for ticker in self._ledger:
            s = self.consecutive_losses(ticker, as_of_date)
            if s > 0:
                out[ticker] = {
                    "streak": s,
                    "multiplier": self.size_multiplier(ticker, as_of_date),
                }
        return out
