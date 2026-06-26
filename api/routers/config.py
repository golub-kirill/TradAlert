"""Read the live strategy config, and write a small whitelist of operational knobs.

Reads return the full ``filters.yaml`` + ``settings.yaml`` for display. Writes are
deliberately narrow: only operational knobs (live risk-budget awareness, the
advisory event-risk window, Telegram notification toggles) are editable. The
edge-defining parameters stay locked here — they're changed in the YAML with a
regression check, never silently from the panel.

A write is a SURGICAL single-line edit: ruamel resolves the key's exact line and
column, then only that one value token is rewritten in the raw text (the inline
comment is kept and re-aligned). Nothing else in the file is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import ROOT
from api.deps import load_yaml

router = APIRouter(tags=["config"])

CONFIG = ROOT / "config"

# dotted key -> (file, type, (min, max) | None). The file root (settings/filters)
# is the first segment; the remainder is the nested path inside that YAML.
_EDITABLE: dict[str, tuple[str, type, tuple[float, float] | None]] = {
    "settings.risk.max_open_risk": ("settings", float, (0.5, 50.0)),
    "settings.scanner.event_risk_within_days": ("settings", int, (0, 60)),
    "settings.telegram.enabled": ("settings", bool, None),
    "settings.telegram.send_stand_down": ("settings", bool, None),
}


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

    # Stage every file's new text in memory and validate ALL edits first, so a
    # failure can't leave a multi-file update half-applied; commit atomically after.
    staged: list[tuple[Path, str]] = []
    written: list[str] = []
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
            except (KeyError, TypeError, AttributeError):
                raise HTTPException(500, f"cannot locate '{key}' in {file}.yaml")
            text = _surgical_set(text, pos[2], pos[3], _token(val))
            written.append(key)
        staged.append((path, text))

    for path, text in staged:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:  # preserve newlines verbatim
                f.write(text)
            os.replace(tmp, path)
        except Exception as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise HTTPException(500, f"cannot write {path.name}: {exc}")

    return {"ok": True, "written": sorted(written)}
