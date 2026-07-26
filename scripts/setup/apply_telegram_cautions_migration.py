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

DDL = """
CREATE TABLE IF NOT EXISTS telegram_cautions (
    id      INT       NOT NULL AUTO_INCREMENT,
    run_id  INT       NOT NULL,
    tickers TEXT      NOT NULL,
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_telegram_cautions_run (run_id),
    CONSTRAINT fk_telegram_cautions_run
        FOREIGN KEY (run_id) REFERENCES scan_runs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def main() -> int:
    apply = "--apply" in sys.argv
    conn = connect()
    cur = conn.cursor()

    cur.execute("SHOW TABLES LIKE 'telegram_cautions'")
    if cur.fetchone():
        print("telegram_cautions already exists — nothing to do.")
        conn.close()
        return 0

    if not apply:
        print("telegram_cautions is MISSING. The caution dedup is inert until it "
              "exists (fails open, repeats every scan).\n")
        print(DDL.strip())
        print("\nDRY RUN — re-run with --apply to create it.")
        conn.close()
        return 0

    cur.execute(DDL)
    conn.commit()
    cur.execute("SHOW CREATE TABLE telegram_cautions")
    row = cur.fetchone()
    print("created:\n")
    print(row[1] if row else "(no output)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
