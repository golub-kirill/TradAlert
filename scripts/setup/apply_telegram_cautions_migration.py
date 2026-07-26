#!/usr/bin/env python3
"""Apply the telegram_cautions migration (docs/backtest_out/MIGRATIONS_2026-07-24.md).

Creates the delivery journal the regime-caution dedup reads. Until the table
exists the dedup fails open — the caution repeats every scan, which is the
pre-fix behaviour, so running this is what actually activates PR #23's purpose.

Idempotent (CREATE TABLE IF NOT EXISTS) and safe to re-run. Reads credentials
from config/secrets.env like every other entry point. No restart needed
afterwards: the scanner is a fresh process per run.

    python scripts/setup/apply_telegram_cautions_migration.py          # dry run
    python scripts/setup/apply_telegram_cautions_migration.py --apply
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / "config" / "secrets.env")
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from persistence.db_conn import connect  # noqa: E402

TABLE = "telegram_cautions"
SCHEMA = _ROOT / "data" / "scan_schema.sql"


def ddl() -> str:
    """The CREATE TABLE for ``TABLE``, read from data/scan_schema.sql.

    Single source of truth on purpose: a second copy inlined here would drift the
    moment the table gains a column, and it would drift in the direction that
    matters — fresh deploys get the schema file, only the EXISTING live DB runs
    this script.
    """
    text = SCHEMA.read_text(encoding="utf-8")
    marker = f"CREATE TABLE IF NOT EXISTS {TABLE}"
    start = text.find(marker)
    if start < 0:
        raise SystemExit(f"{marker} not found in {SCHEMA} — schema and migration "
                         "are out of sync; fix the schema file first.")
    end = text.find(";", start)
    if end < 0:
        raise SystemExit(f"unterminated CREATE TABLE for {TABLE} in {SCHEMA}")
    return text[start:end].strip()


def main() -> int:
    apply = "--apply" in sys.argv
    statement = ddl()          # fails loudly if the schema file lost the table
    conn = connect()
    cur = conn.cursor()

    cur.execute(f"SHOW TABLES LIKE '{TABLE}'")
    if cur.fetchone():
        print(f"{TABLE} already exists — nothing to do.")
        conn.close()
        return 0

    if not apply:
        print(f"{TABLE} is MISSING. The caution dedup is inert until it exists "
              "(fails open, repeats every scan).\n")
        print(f"-- from {SCHEMA.relative_to(_ROOT)}")
        print(statement)
        print("\nDRY RUN — re-run with --apply to create it.")
        conn.close()
        return 0

    cur.execute(statement)
    conn.commit()
    cur.execute(f"SHOW CREATE TABLE {TABLE}")
    row = cur.fetchone()
    print("created:\n")
    print(row[1] if row else "(no output)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
