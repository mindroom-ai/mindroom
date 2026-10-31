"""Compatibility upgrades for persisted script-run databases."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

_SCRIPT_RUN_COLUMN_MIGRATIONS = (
    # LEGACY_COMPAT: Script-run rows without a recovery signature column.
    # Legacy format: Script-run rows lacked recovery_signature.
    # Last legacy release: v2026.9.123; replacement: v2026.9.124 added recovery_signature for adopting a running worker.
    # Handling: Add the absent column in place; old rows read it as null, so a restart never adopts their workers.
    # Coverage: tests/test_script_run_store.py::test_run_store_migrates_existing_table_without_recovery_signature.
    ("recovery_signature", "ALTER TABLE script_runs ADD COLUMN recovery_signature TEXT"),
    # LEGACY_COMPAT: Script-run rows without resource snapshot columns.
    # Legacy format: Script-run rows lacked resource_profile, resource_requests_json, and resource_limits_json.
    # Last legacy release: v2026.8.96; replacement: v2026.8.97 added all three resource snapshot columns.
    # Handling: Add absent columns in place, preserving old rows and defaulting their resource snapshots to empty.
    # Coverage: tests/test_script_run_store.py::test_run_store_migrates_existing_table_for_resource_snapshots.
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
