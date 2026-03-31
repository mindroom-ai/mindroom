"""One-time migration: fix double-encoded JSON in Agno SQLite session databases.

Agno's ``serialize_session_json_fields`` applied ``json.dumps()`` before
inserting into SQLAlchemy ``JSON`` columns, which serialized *again* —
producing double-encoded strings in the database. This script reads
every session row, detects double-encoded fields, re-writes them as
properly-encoded JSON, and reports statistics.

Usage::

    python scripts/fix_double_encoded_sessions.py [--storage-path PATH] [--dry-run]

Default storage path: ``~/.mindroom-chat/mindroom_data``
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

_JSON_FIELDS = (
    "session_data",
    "agent_data",
    "team_data",
    "workflow_data",
    "metadata",
    "summary",
    "runs",
)


def _is_double_encoded(raw: str | None) -> bool:
    """Return True if *raw* is a JSON string wrapping another JSON value."""
    if raw is None:
        return False
    try:
        outer = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(outer, str):
        return False
    try:
        json.loads(outer)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


def _fix_value(raw: str) -> str:
    """Peel one layer of JSON encoding and return the corrected raw string."""
    return json.loads(raw)


def fix_database(db_path: Path, *, dry_run: bool = False) -> tuple[int, int, int]:
    """Fix double-encoded JSON fields in a single session database."""
    if not db_path.exists():
        return 0, 0, 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agno_sessions'",
    )
    if cursor.fetchone() is None:
        conn.close()
        return 0, 0, 0

    cursor.execute("PRAGMA table_info(agno_sessions)")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    fields_to_fix = [field for field in _JSON_FIELDS if field in existing_cols]
    if not fields_to_fix:
        conn.close()
        return 0, 0, 0

    cols_select = ", ".join(["session_id", *fields_to_fix])
    cursor.execute(f"SELECT {cols_select} FROM agno_sessions")  # noqa: S608
    rows = cursor.fetchall()

    sessions_checked = len(rows)
    sessions_fixed = 0
    bytes_saved = 0

    for row in rows:
        updates: dict[str, str] = {}
        for field in fields_to_fix:
            raw = row[field]
            if _is_double_encoded(raw):
                fixed = _fix_value(raw)
                bytes_saved += len(raw) - len(fixed)
                updates[field] = fixed

        if updates:
            sessions_fixed += 1
            if not dry_run:
                set_clause = ", ".join(f"{column} = ?" for column in updates)
                values = [*updates.values(), row["session_id"]]
                cursor.execute(
                    f"UPDATE agno_sessions SET {set_clause} WHERE session_id = ?",  # noqa: S608
                    values,
                )

    if not dry_run:
        conn.commit()
    conn.close()
    return sessions_checked, sessions_fixed, bytes_saved


def main() -> None:
    """CLI entry point for the migration script."""
    parser = argparse.ArgumentParser(
        description="Fix double-encoded JSON in Agno SQLite session databases.",
    )
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path.home() / ".mindroom-chat" / "mindroom_data",
        help="Root storage path (default: ~/.mindroom-chat/mindroom_data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fixed without modifying databases.",
    )
    args = parser.parse_args()

    storage: Path = args.storage_path
    db_files = sorted(storage.glob("agents/*/sessions/*.db"))
    if not db_files:
        db_files = sorted(storage.glob("sessions/*.db"))

    if not db_files:
        print(f"No session databases found under {storage}")
        sys.exit(0)

    print(f"Found {len(db_files)} session database(s) under {storage}")
    if args.dry_run:
        print("DRY RUN — no files will be modified.\n")
    else:
        print()

    total_checked = 0
    total_fixed = 0
    total_bytes = 0

    for db_path in db_files:
        backup_path = db_path.with_suffix(".db.bak")
        if not args.dry_run and not backup_path.exists():
            shutil.copy2(db_path, backup_path)

        checked, fixed, saved = fix_database(db_path, dry_run=args.dry_run)
        total_checked += checked
        total_fixed += fixed
        total_bytes += saved

        status = f"{checked} sessions, {fixed} fixed, {saved:,} bytes saved"
        print(f"  {db_path.relative_to(storage)}: {status}")

    print(f"\nTotal: {total_checked} sessions checked, {total_fixed} fixed, {total_bytes:,} bytes saved")
    if args.dry_run:
        print("Re-run without --dry-run to apply fixes.")


if __name__ == "__main__":
    main()
