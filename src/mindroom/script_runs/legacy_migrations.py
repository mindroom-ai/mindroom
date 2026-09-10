"""Compatibility upgrades for persisted script-run databases."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

_SCRIPT_RUN_COLUMN_MIGRATIONS = (
    ("resource_profile", "ALTER TABLE script_runs ADD COLUMN resource_profile TEXT"),
    (
        "resource_requests_json",
        "ALTER TABLE script_runs ADD COLUMN resource_requests_json TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        "resource_limits_json",
        "ALTER TABLE script_runs ADD COLUMN resource_limits_json TEXT NOT NULL DEFAULT '{}'",
    ),
)


def migrate_legacy_script_run_columns(connection: sqlite3.Connection) -> None:
    """Add columns absent from script-run databases created by older releases."""
    existing_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(script_runs)").fetchall()}
    for column_name, statement in _SCRIPT_RUN_COLUMN_MIGRATIONS:
        if column_name not in existing_columns:
            connection.execute(statement)
