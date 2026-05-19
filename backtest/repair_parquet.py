#!/usr/bin/env python3
"""
repair_parquet.py — Re-save all cached parquet files in cross-platform format.

Run this ONCE from Windows (PyCharm terminal or cmd) after upgrading pyarrow
or if the Linux dev sandbox cannot read the price cache:

    python backtest/repair_parquet.py

What it does
------------
• Reads every *.parquet file in data/prices/ using the current pyarrow.
• Re-writes it in place using pandas to_parquet() with:
    - engine        = 'pyarrow'
    - compression   = 'snappy'
    - write_page_index = False   (pyarrow ≥ 18 writes page indexes by default;
                                  older readers may not parse the footer correctly)
    - version       = '2.4'     (stable Parquet spec version, wide compatibility)
• Verifies the last 4 bytes are b'PAR1' before accepting the rewrite.

Files that cannot be read are reported and skipped (originals are untouched).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in [str(_ROOT), str(_ROOT / "src")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
import pyarrow as pa

CACHE_DIR = _ROOT / "data" / "prices"

WRITE_KWARGS: dict = dict(
    engine="pyarrow",
    compression="snappy",
)

# write_page_index was added in pyarrow 12; guard for older installs
_pq_version = tuple(int(x) for x in pa.__version__.split(".")[:2])
if _pq_version >= (12, 0):
    WRITE_KWARGS["write_page_index"] = False


def _validate_magic(path: Path) -> bool:
    """Return True iff the file ends with the Parquet magic bytes PAR1."""
    try:
        with open(path, "rb") as fh:
            fh.seek(-4, 2)
            return fh.read(4) == b"PAR1"
    except OSError:
        return False


def repair_one(path: Path, dry_run: bool = False) -> str:
    """
    Re-save a single parquet file.  Returns a status string.
    """
    # Check if already valid
    if _validate_magic(path):
        return "ok (already valid)"

    # Try to read
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        # pyarrow can't read it from the current platform — try raw bytes approach
        try:
            raw = path.read_bytes()
            import io
            buf = io.BytesIO(raw)
            df = pd.read_parquet(buf)
        except Exception:
            return f"SKIP — unreadable: {exc}"

    if df.empty:
        return "SKIP — empty DataFrame"

    if dry_run:
        return f"would rewrite ({len(df)} rows)"

    tmp = path.with_suffix(".parquet.tmp")
    try:
        df.to_parquet(tmp, **WRITE_KWARGS)
        if not _validate_magic(tmp):
            tmp.unlink(missing_ok=True)
            return "ERROR — rewrite did not produce valid footer"
        tmp.replace(path)
        return f"repaired ({len(df)} rows)"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return f"ERROR — {exc}"


def main(dry_run: bool = False) -> None:
    files = sorted(CACHE_DIR.glob("*.parquet"))
    if not files:
        print(f"No parquet files found in {CACHE_DIR}")
        return

    print(f"Scanning {len(files)} parquet files in {CACHE_DIR}")
    if dry_run:
        print("(DRY RUN — no files will be modified)\n")

    ok = repaired = skipped = errors = 0
    for path in files:
        status = repair_one(path, dry_run=dry_run)
        tag = (
            "✓" if status.startswith("ok") or status.startswith("repaired")
            else "⚠" if status.startswith("SKIP")
            else "✗"
        )
        print(f"  {tag}  {path.name:<30}  {status}")
        if status.startswith("ok"):
            ok += 1
        elif status.startswith("repaired") or status.startswith("would"):
            repaired += 1
        elif status.startswith("SKIP"):
            skipped += 1
        else:
            errors += 1

    print(f"\nDone: {ok} already valid · {repaired} repaired · "
          f"{skipped} skipped · {errors} errors")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Re-save parquet cache files for cross-platform compatibility")
    p.add_argument("--dry-run", action="store_true", help="Report without modifying files")
    args = p.parse_args()
    main(dry_run=args.dry_run)
