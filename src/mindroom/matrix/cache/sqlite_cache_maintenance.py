"""SQLite schema migration, integrity repair, compaction, and storage diagnostics."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from .cache_maintenance import NONTERMINAL_STREAM_STATUSES, TERMINAL_STREAM_STATUSES, CacheMaintenanceReport
from .sqlite_streaming_compaction import compact_superseded_streaming_edits

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite


async def migrate_version_10_thread_events(db: aiosqlite.Connection) -> int:
    """Normalize a complete version-10 thread snapshot without losing cached events."""
    await db.execute("ALTER TABLE events ADD COLUMN write_seq INTEGER NOT NULL DEFAULT 0")
    await db.execute("UPDATE events SET write_seq = rowid")

    await db.execute("ALTER TABLE thread_events RENAME TO thread_events_v10")
    await db.execute("DROP INDEX IF EXISTS idx_thread_events_room_thread_ts")
    normalized_thread_payload_rows = await _scalar_count(db, "SELECT COUNT(*) FROM thread_events_v10")
    await db.execute(
        """
        CREATE TABLE thread_events (
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            write_seq INTEGER NOT NULL,
            PRIMARY KEY (room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX idx_thread_events_room_thread_ts
        ON thread_events(room_id, thread_id, origin_server_ts)
        """,
    )
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        SELECT DISTINCT
            thread_events_v10.room_id,
            thread_events_v10.thread_id,
            NULL,
            ?,
            'schema_migration_missing_thread_event_source'
        FROM thread_events_v10
        WHERE NOT EXISTS (
            SELECT 1
            FROM events
            WHERE events.event_id = thread_events_v10.event_id
                AND events.room_id = thread_events_v10.room_id
        )
        ON CONFLICT(room_id, thread_id) DO UPDATE SET
            validated_at = NULL,
            invalidated_at = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE thread_cache_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE thread_cache_state.invalidation_reason
            END
        """,
        (time.time(),),
    )
    await db.execute(
        """
        INSERT INTO thread_events(room_id, thread_id, event_id, origin_server_ts, write_seq)
        SELECT
            thread_events_v10.room_id,
            thread_events_v10.thread_id,
            thread_events_v10.event_id,
            thread_events_v10.origin_server_ts,
            (SELECT COALESCE(MAX(write_seq), 0) FROM events) + thread_events_v10.rowid
        FROM thread_events_v10
        JOIN events
            ON events.event_id = thread_events_v10.event_id
            AND events.room_id = thread_events_v10.room_id
        ORDER BY thread_events_v10.rowid
        """,
    )
    await db.execute("DROP TABLE thread_events_v10")
    return normalized_thread_payload_rows


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
        WHERE events.event_id = event_edits.edit_event_id
            AND events.room_id = event_edits.room_id
    )
"""

_ORPHAN_THREAD_INDEX_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM events
        WHERE events.event_id = event_threads.event_id
            AND events.room_id = event_threads.room_id
    )
    AND NOT (
        event_threads.event_id = event_threads.thread_id
        AND (
            EXISTS (
                SELECT 1
                FROM event_threads AS child
                JOIN events AS child_event
                    ON child_event.event_id = child.event_id
                    AND child_event.room_id = child.room_id
                WHERE child.room_id = event_threads.room_id
                    AND child.thread_id = event_threads.thread_id
                    AND child.event_id != child.thread_id
            )
            OR EXISTS (
                SELECT 1
                FROM thread_events AS child_membership
                JOIN events AS child_event
                    ON child_event.event_id = child_membership.event_id
                    AND child_event.room_id = child_membership.room_id
                WHERE child_membership.room_id = event_threads.room_id
                    AND child_membership.thread_id = event_threads.thread_id
                    AND child_membership.event_id != child_membership.thread_id
            )
            OR EXISTS (
                SELECT 1
                FROM compacted_streaming_edits AS archived_child
                WHERE archived_child.room_id = event_threads.room_id
                    AND archived_child.indexed_thread_id = event_threads.thread_id
                    AND archived_child.event_id != event_threads.thread_id
            )
        )
    )
"""

_ORPHAN_THREAD_EVENT_REFERENCE_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM events
        WHERE events.event_id = thread_events.event_id
            AND events.room_id = thread_events.room_id
    )
"""


async def _orphan_edit_index_count(db: aiosqlite.Connection) -> int:
    return await _scalar_count(
        db,
        f"SELECT COUNT(*) FROM event_edits WHERE {_ORPHAN_EDIT_INDEX_PREDICATE}",  # noqa: S608
    )


async def _orphan_thread_index_count(db: aiosqlite.Connection) -> int:
    return await _scalar_count(
        db,
        f"SELECT COUNT(*) FROM event_threads WHERE {_ORPHAN_THREAD_INDEX_PREDICATE}",  # noqa: S608
    )


async def _orphan_thread_event_reference_count(db: aiosqlite.Connection) -> int:
    return await _scalar_count(
        db,
        f"SELECT COUNT(*) FROM thread_events WHERE {_ORPHAN_THREAD_EVENT_REFERENCE_PREDICATE}",  # noqa: S608
    )


async def _repair_orphan_thread_event_references(db: aiosqlite.Connection) -> int:
    stale_at = time.time()
    await db.execute(
        f"""
        INSERT INTO thread_cache_state(
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        SELECT DISTINCT
            thread_events.room_id,
            thread_events.thread_id,
            NULL,
            ?,
            'startup_orphan_thread_event_reference'
        FROM thread_events
        WHERE {_ORPHAN_THREAD_EVENT_REFERENCE_PREDICATE}
        ON CONFLICT(room_id, thread_id) DO UPDATE SET
            validated_at = NULL,
            invalidated_at = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE thread_cache_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN thread_cache_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= thread_cache_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE thread_cache_state.invalidation_reason
            END
        """,  # noqa: S608
        (stale_at,),
    )
    cursor = await db.execute(
        f"DELETE FROM thread_events WHERE {_ORPHAN_THREAD_EVENT_REFERENCE_PREDICATE}",  # noqa: S608
    )
    deleted = 0 if cursor.rowcount is None else int(cursor.rowcount)
    await cursor.close()
    return deleted


async def _repair_orphan_derived_rows(db: aiosqlite.Connection) -> tuple[int, int, int]:
    """Remove invalid derived rows while preserving learned thread-root self mappings."""
    edit_cursor = await db.execute(
        f"DELETE FROM event_edits WHERE {_ORPHAN_EDIT_INDEX_PREDICATE}",  # noqa: S608
    )
    repaired_edit_indexes = 0 if edit_cursor.rowcount is None else int(edit_cursor.rowcount)
    await edit_cursor.close()

    thread_cursor = await db.execute(
        f"DELETE FROM event_threads WHERE {_ORPHAN_THREAD_INDEX_PREDICATE}",  # noqa: S608
    )
    repaired_thread_indexes = 0 if thread_cursor.rowcount is None else int(thread_cursor.rowcount)
    await thread_cursor.close()

    repaired_thread_event_references = await _repair_orphan_thread_event_references(db)
    return repaired_edit_indexes, repaired_thread_indexes, repaired_thread_event_references


async def _collect_maintenance_report(
    db: aiosqlite.Connection,
    *,
    schema_version: int,
    migrated_from_schema_version: int | None,
    destructive_reset: bool,
    normalized_legacy_thread_payload_rows: int,
    orphan_edit_indexes_before: int,
    orphan_thread_indexes_before: int,
    orphan_thread_event_references_before: int,
    repaired_edit_indexes: int,
    repaired_thread_indexes: int,
    repaired_thread_event_references: int,
    compacted_nonterminal_streaming_edits: int,
) -> CacheMaintenanceReport:
    """Collect current SQLite row/category counts after startup maintenance."""
    nonterminal_placeholders = ",".join("?" for _ in NONTERMINAL_STREAM_STATUSES)
    terminal_placeholders = ",".join("?" for _ in TERMINAL_STREAM_STATUSES)
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
        nonterminal_streaming_edit_rows=await _scalar_count(
            db,
            f"""
            SELECT COUNT(*)
            FROM event_edits
            JOIN events ON events.event_id = event_edits.edit_event_id
            WHERE json_extract(
                events.event_json,
                '$.content."m.new_content"."io.mindroom.stream_status"'
            ) IN ({nonterminal_placeholders})
            """,  # noqa: S608
            tuple(sorted(NONTERMINAL_STREAM_STATUSES)),
        ),
        terminal_streaming_edit_rows=await _scalar_count(
            db,
            f"""
            SELECT COUNT(*)
            FROM event_edits
            JOIN events ON events.event_id = event_edits.edit_event_id
            WHERE json_extract(
                events.event_json,
                '$.content."m.new_content"."io.mindroom.stream_status"'
            ) IN ({terminal_placeholders})
            """,  # noqa: S608
            tuple(sorted(TERMINAL_STREAM_STATUSES)),
        ),
        compacted_streaming_edit_archive_rows=await _scalar_count(
            db,
            "SELECT COUNT(*) FROM compacted_streaming_edits",
        ),
        compacted_streaming_edit_archive_bytes=await _scalar_count(
            db,
            "SELECT COALESCE(SUM(length(event_json_zlib)), 0) FROM compacted_streaming_edits",
        ),
        orphan_edit_indexes_before=orphan_edit_indexes_before,
        orphan_edit_indexes_after=await _orphan_edit_index_count(db),
        orphan_thread_indexes_before=orphan_thread_indexes_before,
        orphan_thread_indexes_after=await _orphan_thread_index_count(db),
        orphan_thread_event_references_before=orphan_thread_event_references_before,
        orphan_thread_event_references_after=await _orphan_thread_event_reference_count(db),
        repaired_edit_indexes=repaired_edit_indexes,
        repaired_thread_indexes=repaired_thread_indexes,
        repaired_thread_event_references=repaired_thread_event_references,
        compacted_nonterminal_streaming_edits=compacted_nonterminal_streaming_edits,
    )


async def run_startup_maintenance(
    db: aiosqlite.Connection,
    *,
    schema_version: int,
    migrated_from_schema_version: int | None,
    destructive_reset: bool,
    normalized_legacy_thread_payload_rows: int,
) -> CacheMaintenanceReport:
    """Audit, safely repair, compact, and recount one SQLite cache transaction."""
    orphan_edit_indexes_before = await _orphan_edit_index_count(db)
    orphan_thread_indexes_before = await _orphan_thread_index_count(db)
    orphan_thread_event_references_before = await _orphan_thread_event_reference_count(db)
    repaired_counts = await _repair_orphan_derived_rows(db)
    compacted = await compact_superseded_streaming_edits(db)
    return await _collect_maintenance_report(
        db,
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        destructive_reset=destructive_reset,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        orphan_edit_indexes_before=orphan_edit_indexes_before,
        orphan_thread_indexes_before=orphan_thread_indexes_before,
        orphan_thread_event_references_before=orphan_thread_event_references_before,
        repaired_edit_indexes=repaired_counts[0],
        repaired_thread_indexes=repaired_counts[1],
        repaired_thread_event_references=repaired_counts[2],
        compacted_nonterminal_streaming_edits=compacted,
    )


async def refresh_runtime_metrics(
    db: aiosqlite.Connection,
    *,
    startup_report: CacheMaintenanceReport,
    db_path: Path,
) -> CacheMaintenanceReport:
    """Refresh current counts while preserving immutable startup repair outcomes."""
    report = await _collect_maintenance_report(
        db,
        schema_version=startup_report.schema_version,
        migrated_from_schema_version=startup_report.migrated_from_schema_version,
        destructive_reset=startup_report.destructive_reset,
        normalized_legacy_thread_payload_rows=startup_report.normalized_legacy_thread_payload_rows,
        orphan_edit_indexes_before=startup_report.orphan_edit_indexes_before,
        orphan_thread_indexes_before=startup_report.orphan_thread_indexes_before,
        orphan_thread_event_references_before=startup_report.orphan_thread_event_references_before,
        repaired_edit_indexes=startup_report.repaired_edit_indexes,
        repaired_thread_indexes=startup_report.repaired_thread_indexes,
        repaired_thread_event_references=startup_report.repaired_thread_event_references,
        compacted_nonterminal_streaming_edits=startup_report.compacted_nonterminal_streaming_edits,
    )
    return with_sqlite_storage_bytes(report, db_path)


def sqlite_storage_bytes(db_path: Path) -> int | None:
    """Return current SQLite main/WAL bytes when filesystem metadata is available."""
    paths = (db_path, db_path.with_name(f"{db_path.name}-wal"))
    try:
        return sum(path.stat().st_size for path in paths if path.exists())
    except OSError:
        return None


def with_sqlite_storage_bytes(report: CacheMaintenanceReport, db_path: Path) -> CacheMaintenanceReport:
    """Attach the committed SQLite file size to a maintenance report."""
    return replace(report, storage_bytes=sqlite_storage_bytes(db_path))
