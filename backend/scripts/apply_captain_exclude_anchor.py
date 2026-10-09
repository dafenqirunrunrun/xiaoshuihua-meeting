"""Apply the `captain_report.exclude_anchor` column to an existing SQLite DB.

Why this exists
---------------
`Base.metadata.create_all()` creates MISSING TABLES but never ALTERs an existing
one. Any database created during ADR-002 Phase 2 already has `captain_report`
without `exclude_anchor`, so after this change every insert would fail with
"no such column". This script closes that gap idempotently.

    backend/.venv/Scripts/python scripts/apply_captain_exclude_anchor.py
    backend/.venv/Scripts/python scripts/apply_captain_exclude_anchor.py --db path/to.db

It is safe to run repeatedly: it checks `PRAGMA table_info` and does nothing when
the column already exists. It does not touch any other table.

Existing rows get `1` (exclude), preserving the older, stricter behaviour — see
`migrations/20260929_captain_exclude_anchor.sql`.
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

_BACKEND = pathlib.Path(__file__).resolve().parent.parent

COLUMN = "exclude_anchor"
TABLE = "captain_report"


def _default_db_path() -> pathlib.Path:
    """Resolve the DB the app would actually use, honouring DATABASE_URL."""
    sys.path.insert(0, str(_BACKEND))
    from app.config import settings  # imported late so --help never needs config

    url = settings.database_url
    if not url.startswith("sqlite:///"):
        sys.exit(f"this script only handles sqlite:/// URLs, got {url!r}")
    path = pathlib.Path(url.removeprefix("sqlite:///"))
    return path if path.is_absolute() else (_BACKEND / path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=None)
    args = parser.parse_args()

    db_path = args.db or _default_db_path()
    if not db_path.exists():
        print(f"database not found: {db_path}")
        print("Nothing to do — a fresh DB gets the column from create_all().")
        return 0

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if TABLE not in tables:
            print(f"{TABLE} does not exist in {db_path}; create_all() will build it.")
            return 0

        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")]
        if COLUMN in columns:
            print(f"{TABLE}.{COLUMN} already present in {db_path} — no change.")
            return 0

        conn.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} BOOLEAN NOT NULL DEFAULT 1"
        )
        conn.commit()
        rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        print(f"Added {TABLE}.{COLUMN} to {db_path} (existing rows set to 1: {rows}).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
