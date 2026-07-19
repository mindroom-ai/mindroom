"""SQLite schema migration, integrity repair, and storage diagnostics."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .cache_maintenance import CacheMaintenanceReport
from .sqlite_event_cache_events import orphan_thread_index_count, repair_orphan_thread_indexes

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite


async def _scalar_count(
    db: aiosqlite.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    cursor = await db.execute(query, parameters)
    row = await cursor.fetchone()
    await cursor.close()
    return 0 if row is None else int(row[0])


_ORPHAN_EDIT_INDEX_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM events
        WHERE events.principal_id = event_edits.principal_id
            AND events.room_id = event_edits.room_id
            AND events.event_id = event_edits.edit_event_id
    )
"""


async def _orphan_edit_index_count(db: aiosqlite.Connection) -> int:
    return await _scalar_count(
        db,
        f"SELECT COUNT(*) FROM event_edits WHERE {_ORPHAN_EDIT_INDEX_PREDICATE}",  # noqa: S608
    )


async def _repair_orphan_derived_rows(db: aiosqlite.Connection) -> tuple[int, int]:
    """Remove invalid derived rows while preserving learned thread-root self mappings."""
    edit_cursor = await db.execute(
        f"DELETE FROM event_edits WHERE {_ORPHAN_EDIT_INDEX_PREDICATE}",  # noqa: S608
    )
    repaired_edit_indexes = 0 if edit_cursor.rowcount is None else int(edit_cursor.rowcount)
    await edit_cursor.close()

    repaired_thread_indexes = await repair_orphan_thread_indexes(db)
    return repaired_edit_indexes, repaired_thread_indexes


async def _collect_maintenance_report(
    db: aiosqlite.Connection,
    *,
    schema_version: int,
    migrated_from_schema_version: int | None,
    destructive_reset: bool,
    normalized_legacy_thread_payload_rows: int,
    repaired_edit_indexes: int,
    repaired_thread_indexes: int,
) -> CacheMaintenanceReport:
    """Collect current SQLite row/category counts after startup maintenance."""
    return CacheMaintenanceReport(
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        destructive_reset=destructive_reset,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        event_rows=await _scalar_count(db, "SELECT COUNT(*) FROM events"),
        thread_event_reference_rows=await _scalar_count(db, "SELECT COUNT(*) FROM thread_events"),
        edit_index_rows=await _scalar_count(db, "SELECT COUNT(*) FROM event_edits"),
        thread_index_rows=await _scalar_count(db, "SELECT COUNT(*) FROM event_threads"),
        tombstone_rows=await _scalar_count(db, "SELECT COUNT(*) FROM redacted_events"),
        mxc_rows=await _scalar_count(db, "SELECT COUNT(*) FROM mxc_text_cache"),
        thread_state_rows=await _scalar_count(db, "SELECT COUNT(*) FROM thread_cache_state"),
        room_state_rows=await _scalar_count(db, "SELECT COUNT(*) FROM room_cache_state"),
        stale_thread_markers=await _scalar_count(
            db,
            """
            SELECT COUNT(*)
            FROM thread_cache_state
            WHERE invalidated_at IS NOT NULL
                AND (validated_at IS NULL OR invalidated_at >= validated_at)
            """,
        ),
        stale_room_markers=await _scalar_count(
            db,
            "SELECT COUNT(*) FROM room_cache_state WHERE invalidated_at IS NOT NULL",
        ),
        orphan_edit_indexes_after=await _orphan_edit_index_count(db),
        orphan_thread_indexes_after=await orphan_thread_index_count(db),
        repaired_edit_indexes=repaired_edit_indexes,
        repaired_thread_indexes=repaired_thread_indexes,
    )


async def run_startup_maintenance(
    db: aiosqlite.Connection,
    *,
    schema_version: int,
    migrated_from_schema_version: int | None,
    destructive_reset: bool,
    normalized_legacy_thread_payload_rows: int,
) -> CacheMaintenanceReport:
    """Audit, safely repair, and recount one SQLite cache transaction."""
    repaired_counts = await _repair_orphan_derived_rows(db)
    return await _collect_maintenance_report(
        db,
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        destructive_reset=destructive_reset,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        repaired_edit_indexes=repaired_counts[0],
        repaired_thread_indexes=repaired_counts[1],
    )


def _sqlite_storage_bytes(db_path: Path) -> int | None:
    """Return current SQLite main/WAL bytes when filesystem metadata is available."""
    paths = (db_path, db_path.with_name(f"{db_path.name}-wal"))
    try:
        return sum(path.stat().st_size for path in paths if path.exists())
    except OSError:
        return None


def with_sqlite_storage_bytes(report: CacheMaintenanceReport, db_path: Path) -> CacheMaintenanceReport:
    """Attach the committed SQLite file size to a maintenance report."""
    return replace(report, storage_bytes=_sqlite_storage_bytes(db_path))
