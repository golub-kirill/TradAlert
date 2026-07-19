"""Date-literal creep checker for production code.

A calendar date in production is a published fact that belongs in a fetcher,
a cache, or a provenance-commented curated list — not an inline literal (the
CPI 2026-07-15 defect came from a typed literal backed by an invented rule).
This checker inventories `date(Y, M, D)` / `datetime(Y, M, D)` constructor
literals and ISO-date strings in the production roots and fails on any hit
outside the allowlist, so new literals can't creep in silently.

Scope: ``date()``/``datetime()`` constructor literals in .py (the actual
failure mode — ISO strings in code are overwhelmingly docstring examples and
are NOT scanned), plus ISO dates in config/*.yaml.

Allowlisted (each with a reason, reviewed 2026-07-17):
  src/core/macro/calendar.py   the curated offline fallback — every row carries
                               source + as-of provenance and fact-anchor tests
  config/filters.yaml          events.stop_dates — a historical record the
                               backtest replays (rows are never deleted)
  config/watchlist.yaml        survivorship_audit as-of pins — analysis
                               parameters, not calendar facts

Usage:  python scripts/setup/check_date_literals.py   (exit 1 on violations)
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS = ("src", "backtest", "api", "main.py", "telegram_bot.py",
              "position_CLI.py")
SCAN_CONFIG = ("config",)
SKIP_DIRS = {"tests", "scripts", "data", "docs", ".venv", "node_modules"}
ALLOWLIST = {
    Path("src/core/macro/calendar.py"),
    Path("config/filters.yaml"),
    Path("config/watchlist.yaml"),
}
_ISO = re.compile(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in {"date", "datetime"} and len(node.args) >= 3 and all(
                isinstance(a, ast.Constant) and isinstance(a.value, int)
                for a in node.args[:3]):
            y, m, d = (a.value for a in node.args[:3])
            self.hits.append((node.lineno, f"{name}({y}, {m}, {d})"))
        self.generic_visit(node)


def _py_files():
    for root in SCAN_ROOTS:
        p = _ROOT / root
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if not (set(f.relative_to(_ROOT).parts) & SKIP_DIRS):
                    yield f


def main() -> int:
    bad = 0
    allowed = 0
    for f in sorted(_py_files()):
        rel = f.relative_to(_ROOT)
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        v = _Visitor()
        v.visit(tree)
        for ln, txt in v.hits:
            if rel in ALLOWLIST:
                allowed += 1
            else:
                print(f"{rel}:{ln}: date literal -> {txt}")
                bad += 1
    for root in SCAN_CONFIG:
        for f in sorted((_ROOT / root).glob("*.yaml")):
            rel = f.relative_to(_ROOT)
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                # Strip inline comments — a date after ` #` is provenance, not a
                # value the loader reads (the whole point of the curated lists).
                code = line.split(" #", 1)[0]
                if _ISO.search(code) and not code.strip().startswith("#"):
                    if rel in ALLOWLIST:
                        allowed += 1
                    else:
                        print(f"{rel}:{i}: ISO date literal -> {line.strip()[:60]}")
                        bad += 1
    print(f"\n{'FAIL' if bad else 'PASS'}: {bad} unallowlisted date literal(s) "
          f"in production code ({allowed} allowlisted).")
    if bad:
        print("A date is a published fact: fetch it, cache it, or add it to the "
              "curated provenance-commented list — don't inline it.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
