"""Read the live strategy config, and write a whitelist of knobs.

Reads return the full ``filters.yaml`` + ``settings.yaml`` for display.

**Writes are NOT limited to operational knobs.** This docstring used to claim
"the edge-defining parameters stay locked here"; that was false, and the
whitelist below is the evidence — 14 of its 21 keys (``_EDGE_DEFINING``) change
which trades the strategy takes: the MA pair, the stop multiplier and min R:R,
max-hold, the breakeven trigger, the scan gates, the VIX bands, shorts, and the
sector gate. Editing any of them here moves the live strategy away from the
config the +93.53R gate was measured under, with no regression check in the loop.

Kept as a deliberate capability, but audited rather than silent: every write
appends a line to ``data/config_audit.jsonl`` with timestamp, key, old → new
value and whether the key is edge-defining, and the response carries
``requires_regression_check`` listing any edge-defining keys touched. Narrowing
``_EDITABLE`` instead remains an owner call.

A write is a SURGICAL single-line edit: ruamel resolves the key's exact line and
column, then only that one value token is rewritten in the raw text (the inline
comment is kept and re-aligned). Nothing else in the file is touched.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import ROOT
from api.deps import load_yaml

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])

CONFIG = ROOT / "config"

# dotted key -> (file, type, (min, max) | None). The file root (settings/filters)
# is the first segment; the remainder is the nested path inside that YAML.
_EDITABLE: dict[str, tuple[str, type, tuple[float, float] | None]] = {
    # risk / scanner / notifications
    "settings.risk.max_open_risk": ("settings", float, (0.5, 50.0)),
    "settings.scanner.event_risk_within_days": ("settings", int, (0, 60)),
    "settings.telegram.enabled": ("settings", bool, None),
    "settings.telegram.send_stand_down": ("settings", bool, None),
    # layers
    "settings.macro.enabled": ("settings", bool, None),
    "settings.behavioral.enabled": ("settings", bool, None),
    "settings.advisor.enabled": ("settings", bool, None),
    "filters.signals.allow_shorts": ("filters", bool, None),
    "filters.signals.sector_gate.enabled": ("filters", bool, None),
    # scan filters
    "filters.price.min_price": ("filters", float, (0.0, 100000.0)),
    "filters.liquidity.min_dollar_volume_20d": ("filters", int, (0, 100_000_000_000)),
    "filters.volatility.min_atr_pct": ("filters", float, (0.0, 50.0)),
    "filters.volatility.max_atr_pct": ("filters", float, (0.0, 100.0)),
    "filters.trend.ma_fast": ("filters", int, (2, 400)),
    "filters.trend.ma_slow": ("filters", int, (5, 600)),
    "filters.signals.stop_loss.min_rr": ("filters", float, (0.0, 20.0)),
    "filters.signals.stop_loss.atr_multiplier": ("filters", float, (0.1, 20.0)),
    "filters.execution.max_hold_days": ("filters", int, (1, 500)),
    "filters.execution.breakeven_trigger_r": ("filters", float, (0.0, 10.0)),
    "filters.regime.vix_low": ("filters", float, (0.0, 100.0)),
    "filters.regime.vix_high": ("filters", float, (0.0, 100.0)),
}

# Whitelisted keys that change TRADE COMPOSITION — i.e. editing one invalidates
# the regression baseline until a paired A/B is re-run. Every `filters.*` key
# qualifies by construction (the engine reads that file); the `settings.*` keys
# left out are display / notification / layer toggles that do not alter entries.
# NOTE `filters.signals.sector_gate.enabled` is flagged here but EXCLUDED from
# backtest.db._CONFIG_MATCH_EXCLUDE: with an empty sector_map.yaml it changes no
# trades, so it must not invalidate a reference run — but the panel stays
# conservative, because populating the map would make it bite.
_EDGE_DEFINING: frozenset[str] = frozenset(
    k for k in _EDITABLE if k.startswith("filters.")
)

# Append-only provenance log. Not a DB table on purpose: it must survive a DB
# outage, and it is read by humans after the fact ("who moved min_rr?").
AUDIT_LOG = ROOT / "data" / "config_audit.jsonl"


def _record_provenance(entries: list[dict]) -> None:
    """Append one JSON line per changed key. Never raises into the request —
    a full disk must not make a successful config write look like a failure."""
    if not entries:
        return
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, default=str) + "\n")
    except Exception:
        logger.warning("config write succeeded but provenance was not recorded "
                       "(%s): %s", AUDIT_LOG, ", ".join(e["key"] for e in entries),
                       exc_info=True)


@router.get("/config")
def config():
    return {
        "filters": load_yaml("filters.yaml"),
        "settings": load_yaml("settings.yaml"),
        "editable": sorted(_EDITABLE),
    }


class ConfigWrite(BaseModel):
    updates: dict[str, object]


def _coerce(key: str, raw: object):
    _, typ, rng = _EDITABLE[key]
    if typ is bool:
        if not isinstance(raw, bool):
            raise HTTPException(400, f"'{key}' must be true/false")
        return raw
    if isinstance(raw, bool):  # bool is an int subclass — reject for numeric keys
        raise HTTPException(400, f"'{key}' must be a {typ.__name__}")
    try:
        val = typ(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"'{key}' must be a {typ.__name__}")
    if rng and not (rng[0] <= val <= rng[1]):
        raise HTTPException(400, f"'{key}' out of range [{rng[0]}, {rng[1]}]")
    return val


def _token(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _surgical_set(text: str, vl: int, vc: int, new_token: str) -> str:
    """Replace the value token at (line vl, col vc), keeping the inline comment.

    Never adds/removes lines, so caller-supplied line indices stay valid across
    several edits to the same file. Assumes a space-free scalar (our whitelist is
    numbers/bools only).
    """
    lines = text.splitlines(keepends=True)
    line = lines[vl]
    if line.endswith("\r\n"):
        nl, body = "\r\n", line[:-2]
    elif line.endswith("\n"):
        nl, body = "\n", line[:-1]
    else:
        nl, body = "", line
    after = body[vc:]
    i = 0
    while i < len(after) and not after[i].isspace():
        i += 1
    old_token, rest = after[:i], after[i:]
    hash_at = rest.find("#")
    if hash_at >= 0:  # keep the comment in its original column when possible
        target_col = vc + len(old_token) + hash_at
        newgap = target_col - (vc + len(new_token))
        rest = (" " * newgap if newgap >= 1 else " ") + rest[hash_at:]
    lines[vl] = body[:vc] + new_token + rest + nl
    return "".join(lines)


@router.post("/config")
def write_config(body: ConfigWrite):
    if not body.updates:
        raise HTTPException(400, "no updates supplied")
    for key in body.updates:
        if key not in _EDITABLE:
            raise HTTPException(400, f"parameter '{key}' is locked")

    try:
        from ruamel.yaml import YAML
    except Exception:
        raise HTTPException(503, "config write unavailable (ruamel.yaml not installed)")

    by_file: dict[str, dict[str, object]] = {}
    for key, raw in body.updates.items():
        file, _, _ = _EDITABLE[key]
        by_file.setdefault(file, {})[key] = _coerce(key, raw)

    # Stage every file's new text in memory and validate ALL edits first, so a bad
    # edit fails before anything is written; the staged text is committed below.
    staged: list[tuple[Path, str]] = []
    written: list[str] = []
    provenance: list[dict] = []
    for file, items in by_file.items():
        path = CONFIG / f"{file}.yaml"
        try:
            with open(path, encoding="utf-8", newline="") as f:  # newline="" keeps CRLF/LF verbatim
                text = f.read()
        except Exception as exc:
            raise HTTPException(500, f"cannot read {file}.yaml: {exc}")
        try:
            doc = YAML().load(text)
        except Exception as exc:
            raise HTTPException(500, f"cannot parse {file}.yaml: {exc}")

        for key, val in items.items():
            segs = key.split(".")[1:]  # drop the file-root segment
            node = doc
            try:
                for s in segs[:-1]:
                    node = node[s]
                pos = node.lc.data[segs[-1]]  # (key_line, key_col, val_line, val_col)
                old = node[segs[-1]]
            except (KeyError, TypeError, AttributeError):
                raise HTTPException(500, f"cannot locate '{key}' in {file}.yaml")
            text = _surgical_set(text, pos[2], pos[3], _token(val))
            written.append(key)
            # Recorded only after the two-phase commit below succeeds — a staged
            # edit that never lands must not appear in the audit trail.
            provenance.append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "key": key, "old": old, "new": val, "file": f"{file}.yaml",
                "edge_defining": key in _EDGE_DEFINING,
            })
        staged.append((path, text))

    # Two-phase commit: write every temp file first, then os.replace them all, so a
    # write fault leaves the live configs untouched. A fault *between* two replaces
    # is the only residual gap (true multi-file atomicity would need a real txn).
    tmps: list[tuple[Path, Path]] = []  # (tmp, final)
    try:
        for path, text in staged:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as f:  # preserve newlines verbatim
                f.write(text)
            tmps.append((tmp, path))
        for tmp, path in tmps:
            os.replace(tmp, path)
    except Exception as exc:
        for tmp, _ in tmps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # A fault BETWEEN two os.replace calls leaves one file updated and one
        # not — the documented residual gap, and precisely when "what landed?"
        # is hardest to reconstruct. Record the attempt rather than nothing.
        _record_provenance([{**e, "outcome": "failed", "error": str(exc)}
                            for e in provenance])
        raise HTTPException(500, f"cannot write config: {exc}")

    _record_provenance([{**e, "outcome": "applied"} for e in provenance])
    edge = sorted(k for k in written if k in _EDGE_DEFINING)
    if edge:
        logger.warning("[config] EDGE-DEFINING keys changed from the panel — the "
                       "regression baseline no longer describes the live config "
                       "until a paired A/B is re-run: %s", ", ".join(edge))
    return {"ok": True, "written": sorted(written),
            "requires_regression_check": edge}
