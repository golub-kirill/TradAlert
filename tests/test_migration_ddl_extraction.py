"""The telegram_cautions migration reads its DDL from data/scan_schema.sql.

Single source of truth: a second inlined copy would drift the moment the table
gained a column, and it would drift in the direction that matters — fresh deploys
read the schema file, only the EXISTING live DB runs the migration script.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "setup" / "apply_telegram_cautions_migration.py"


def _module():
    spec = importlib.util.spec_from_file_location("_mig", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mig():
    return _module()


def test_extracts_the_whole_statement_from_the_schema(mig):
    stmt = mig.ddl()
    assert stmt.startswith("CREATE TABLE IF NOT EXISTS telegram_cautions")
    assert "ENGINE=InnoDB" in stmt                 # reached the end of the block
    assert stmt.count("(") == stmt.count(")")      # not truncated mid-definition
    assert not stmt.rstrip().endswith(";")         # terminator stripped for execute()


def test_statement_matches_the_shipped_schema_file(mig):
    """If these drift, the migration would create a different table than a fresh
    deploy gets — the exact failure the extraction exists to prevent."""
    schema = (_ROOT / "data" / "scan_schema.sql").read_text(encoding="utf-8")
    assert mig.ddl() in schema


def test_semicolon_inside_a_comment_does_not_truncate(mig, tmp_path, monkeypatch):
    """Terminating on the first bare ';' would cut the DDL in half here."""
    schema = tmp_path / "scan_schema.sql"
    schema.write_text(
        "CREATE TABLE IF NOT EXISTS telegram_cautions (\n"
        "    id      INT NOT NULL AUTO_INCREMENT,   -- surrogate key; see notes\n"
        "    tickers TEXT NOT NULL,\n"
        "    PRIMARY KEY (id)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n",
        encoding="utf-8")
    monkeypatch.setattr(mig, "SCHEMA", schema)
    stmt = mig.ddl()
    assert "PRIMARY KEY (id)" in stmt and "ENGINE=InnoDB" in stmt
    assert stmt.count("(") == stmt.count(")")


def test_semicolon_in_the_terminating_line_comment_does_not_leak(mig, tmp_path, monkeypatch):
    """The nastier half of the same case: the comment sits on the line that ENDS
    the statement, so a search over raw text picks the comment's ';' and leaves
    the real terminator embedded with a comment fragment glued on."""
    schema = tmp_path / "scan_schema.sql"
    schema.write_text(
        "CREATE TABLE IF NOT EXISTS telegram_cautions (\n"
        "    id INT NOT NULL,\n"
        "    PRIMARY KEY (id)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;  -- created 2026; see MIGRATIONS\n",
        encoding="utf-8")
    monkeypatch.setattr(mig, "SCHEMA", schema)
    stmt = mig.ddl()
    assert ";" not in stmt                      # terminator stripped, none embedded
    assert "--" not in stmt                     # no comment fragment carried over
    assert stmt.endswith("ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    assert stmt.count("(") == stmt.count(")")


def test_missing_table_fails_loudly(mig, tmp_path, monkeypatch):
    schema = tmp_path / "scan_schema.sql"
    schema.write_text("CREATE TABLE IF NOT EXISTS something_else (id INT);\n",
                      encoding="utf-8")
    monkeypatch.setattr(mig, "SCHEMA", schema)
    with pytest.raises(SystemExit, match="out of sync"):
        mig.ddl()


def test_unterminated_statement_fails_loudly(mig, tmp_path, monkeypatch):
    schema = tmp_path / "scan_schema.sql"
    schema.write_text("CREATE TABLE IF NOT EXISTS telegram_cautions (\n  id INT\n",
                      encoding="utf-8")
    monkeypatch.setattr(mig, "SCHEMA", schema)
    with pytest.raises(SystemExit, match="unterminated"):
        mig.ddl()
