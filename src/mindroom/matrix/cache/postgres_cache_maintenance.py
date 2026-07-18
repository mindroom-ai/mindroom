"""PostgreSQL schema migration, integrity repair, compaction, and diagnostics."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .cache_maintenance import NONTERMINAL_STREAM_STATUSES, TERMINAL_STREAM_STATUSES, CacheMaintenanceReport
from .postgres_cursor import fetchone, rowcount
from .postgres_streaming_compaction import compact_superseded_streaming_edits

if TYPE_CHECKING:
    from typing import LiteralString

    from psycopg import AsyncConnection


async def migrate_postgres_schema(
    db: AsyncConnection,
    *,
    namespace: str,
    current_schema_version: int | None,
    target_schema_version: int,
) -> int | None:
    """Transactionally normalize one namespace while upgrading the shared schema."""
    if current_schema_version not in {None, 1, target_schema_version}:
        msg = (
            "PostgreSQL Matrix event cache schema version "
            f"{current_schema_version} is not compatible with expected version {target_schema_version}"
        )
        raise RuntimeError(msg)

    migrated_from = 1 if current_schema_version == 1 else None
    if migrated_from is not None:
        await db.execute(
            """
            ALTER TABLE mindroom_event_cache_thread_events
            ALTER COLUMN event_json DROP NOT NULL
            """,
        )

    await db.execute(
        """
        UPDATE mindroom_event_cache_thread_events
        SET event_json = NULL
        WHERE namespace = %s AND event_json IS NOT NULL
        """,
        (namespace,),
    )
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_metadata(key, value)
        VALUES ('schema_version', %s)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(target_schema_version),),
    )
    return migrated_from


async def _count(
    db: AsyncConnection,
    query: LiteralString,
    parameters: tuple[object, ...],
) -> int:
    row = await fetchone(db, query, parameters)
    return 0 if row is None else int(row[0])


async def _orphan_edit_index_count(db: AsyncConnection, *, namespace: str) -> int:
    return await _count(
        db,
        """
        SELECT COUNT(*)
        FROM mindroom_event_cache_event_edits AS event_edits
        WHERE event_edits.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = event_edits.namespace
                    AND events.event_id = event_edits.edit_event_id
                    AND events.room_id = event_edits.room_id
            )
        """,
        (namespace,),
    )


async def _orphan_thread_index_count(db: AsyncConnection, *, namespace: str) -> int:
    return await _count(
        db,
        """
        SELECT COUNT(*)
        FROM mindroom_event_cache_event_threads AS event_threads
        WHERE event_threads.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = event_threads.namespace
                    AND events.event_id = event_threads.event_id
                    AND events.room_id = event_threads.room_id
            )
            AND NOT (
                event_threads.event_id = event_threads.thread_id
                AND (
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_event_threads AS child
                        JOIN mindroom_event_cache_events AS child_event
                            ON child_event.namespace = child.namespace
                            AND child_event.event_id = child.event_id
                            AND child_event.room_id = child.room_id
                        WHERE child.namespace = event_threads.namespace
                            AND child.room_id = event_threads.room_id
                            AND child.thread_id = event_threads.thread_id
                            AND child.event_id != child.thread_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_thread_events AS child_membership
                        JOIN mindroom_event_cache_events AS child_event
                            ON child_event.namespace = child_membership.namespace
                            AND child_event.event_id = child_membership.event_id
                            AND child_event.room_id = child_membership.room_id
                        WHERE child_membership.namespace = event_threads.namespace
                            AND child_membership.room_id = event_threads.room_id
                            AND child_membership.thread_id = event_threads.thread_id
                            AND child_membership.event_id != child_membership.thread_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_compacted_streaming_edits AS archived_child
                        WHERE archived_child.namespace = event_threads.namespace
                            AND archived_child.room_id = event_threads.room_id
                            AND archived_child.indexed_thread_id = event_threads.thread_id
                            AND archived_child.event_id != event_threads.thread_id
                    )
                )
            )
        """,
        (namespace,),
    )


async def _orphan_thread_event_reference_count(db: AsyncConnection, *, namespace: str) -> int:
    return await _count(
        db,
        """
        SELECT COUNT(*)
        FROM mindroom_event_cache_thread_events AS thread_events
        WHERE thread_events.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = thread_events.namespace
                    AND events.event_id = thread_events.event_id
                    AND events.room_id = thread_events.room_id
            )
        """,
        (namespace,),
    )


async def _repair_orphan_derived_rows(
    db: AsyncConnection,
    *,
    namespace: str,
) -> tuple[int, int, int]:
    """Remove invalid derived rows while preserving learned thread-root mappings."""
    repaired_edit_indexes = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_event_edits AS event_edits
        WHERE event_edits.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = event_edits.namespace
                    AND events.event_id = event_edits.edit_event_id
                    AND events.room_id = event_edits.room_id
            )
        """,
        (namespace,),
    )
    repaired_thread_indexes = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_event_threads AS event_threads
        WHERE event_threads.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = event_threads.namespace
                    AND events.event_id = event_threads.event_id
                    AND events.room_id = event_threads.room_id
            )
            AND NOT (
                event_threads.event_id = event_threads.thread_id
                AND (
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_event_threads AS child
                        JOIN mindroom_event_cache_events AS child_event
                            ON child_event.namespace = child.namespace
                            AND child_event.event_id = child.event_id
                            AND child_event.room_id = child.room_id
                        WHERE child.namespace = event_threads.namespace
                            AND child.room_id = event_threads.room_id
                            AND child.thread_id = event_threads.thread_id
                            AND child.event_id != child.thread_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_thread_events AS child_membership
                        JOIN mindroom_event_cache_events AS child_event
                            ON child_event.namespace = child_membership.namespace
                            AND child_event.event_id = child_membership.event_id
                            AND child_event.room_id = child_membership.room_id
                        WHERE child_membership.namespace = event_threads.namespace
                            AND child_membership.room_id = event_threads.room_id
                            AND child_membership.thread_id = event_threads.thread_id
                            AND child_membership.event_id != child_membership.thread_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_compacted_streaming_edits AS archived_child
                        WHERE archived_child.namespace = event_threads.namespace
                            AND archived_child.room_id = event_threads.room_id
                            AND archived_child.indexed_thread_id = event_threads.thread_id
                            AND archived_child.event_id != event_threads.thread_id
                    )
                )
            )
        """,
        (namespace,),
    )
    stale_at = time.time()
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_thread_state(
            namespace,
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        SELECT DISTINCT
            thread_events.namespace,
            thread_events.room_id,
            thread_events.thread_id,
            NULL::DOUBLE PRECISION,
            %s,
            'startup_orphan_thread_event_reference'
        FROM mindroom_event_cache_thread_events AS thread_events
        WHERE thread_events.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = thread_events.namespace
                    AND events.event_id = thread_events.event_id
                    AND events.room_id = thread_events.room_id
            )
        ON CONFLICT(namespace, room_id, thread_id) DO UPDATE SET
            invalidated_at = CASE
                WHEN mindroom_event_cache_thread_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= mindroom_event_cache_thread_state.invalidated_at
                    THEN excluded.invalidated_at
                ELSE mindroom_event_cache_thread_state.invalidated_at
            END,
            invalidation_reason = CASE
                WHEN mindroom_event_cache_thread_state.invalidated_at IS NULL
                    OR excluded.invalidated_at >= mindroom_event_cache_thread_state.invalidated_at
                    THEN excluded.invalidation_reason
                ELSE mindroom_event_cache_thread_state.invalidation_reason
            END
        """,
        (stale_at, namespace),
    )
    repaired_thread_event_references = await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_thread_events AS thread_events
        WHERE thread_events.namespace = %s
            AND NOT EXISTS (
                SELECT 1
                FROM mindroom_event_cache_events AS events
                WHERE events.namespace = thread_events.namespace
                    AND events.event_id = thread_events.event_id
                    AND events.room_id = thread_events.room_id
            )
        """,
        (namespace,),
    )
    return repaired_edit_indexes, repaired_thread_indexes, repaired_thread_event_references


async def _collect_maintenance_report(
    db: AsyncConnection,
    *,
    namespace: str,
    schema_version: int,
    migrated_from_schema_version: int | None,
    orphan_counts_before: tuple[int, int, int],
    repaired_counts: tuple[int, int, int],
    compacted_nonterminal_streaming_edits: int,
) -> CacheMaintenanceReport:
    """Collect log-safe backend and namespace storage diagnostics."""
    return CacheMaintenanceReport(
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        storage_bytes=await _count(db, "SELECT pg_database_size(current_database())", ()),
        namespace_payload_bytes=await _count(
            db,
            """
            SELECT
                COALESCE((
                    SELECT SUM(octet_length(event_json))
                    FROM mindroom_event_cache_events
                    WHERE namespace = %s
                ), 0)
                + COALESCE((
                    SELECT SUM(octet_length(event_json_zlib))
                    FROM mindroom_event_cache_compacted_streaming_edits
                    WHERE namespace = %s
                ), 0)
                + COALESCE((
                    SELECT SUM(octet_length(text_content))
                    FROM mindroom_event_cache_mxc_text
                    WHERE namespace = %s
                ), 0)
            """,
            (namespace, namespace, namespace),
        ),
        event_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_events WHERE namespace = %s",
            (namespace,),
        ),
        thread_event_reference_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_thread_events WHERE namespace = %s",
            (namespace,),
        ),
        edit_index_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_event_edits WHERE namespace = %s",
            (namespace,),
        ),
        thread_index_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_event_threads WHERE namespace = %s",
            (namespace,),
        ),
        tombstone_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_redacted_events WHERE namespace = %s",
            (namespace,),
        ),
        mxc_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_mxc_text WHERE namespace = %s",
            (namespace,),
        ),
        thread_state_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_thread_state WHERE namespace = %s",
            (namespace,),
        ),
        room_state_rows=await _count(
            db,
            "SELECT COUNT(*) FROM mindroom_event_cache_room_state WHERE namespace = %s",
            (namespace,),
        ),
        stale_thread_markers=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_thread_state
            WHERE namespace = %s
                AND invalidated_at IS NOT NULL
                AND (validated_at IS NULL OR invalidated_at >= validated_at)
            """,
            (namespace,),
        ),
        stale_room_markers=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_room_state
            WHERE namespace = %s AND invalidated_at IS NOT NULL
            """,
            (namespace,),
        ),
        nonterminal_streaming_edit_rows=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_event_edits AS edits
            JOIN mindroom_event_cache_events AS events
                ON events.namespace = edits.namespace AND events.event_id = edits.edit_event_id
            WHERE edits.namespace = %s
                AND events.event_json::jsonb
                    -> 'content' -> 'm.new_content' ->> 'io.mindroom.stream_status' = ANY(%s)
            """,
            (namespace, sorted(NONTERMINAL_STREAM_STATUSES)),
        ),
        terminal_streaming_edit_rows=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_event_edits AS edits
            JOIN mindroom_event_cache_events AS events
                ON events.namespace = edits.namespace AND events.event_id = edits.edit_event_id
            WHERE edits.namespace = %s
                AND events.event_json::jsonb
                    -> 'content' -> 'm.new_content' ->> 'io.mindroom.stream_status' = ANY(%s)
            """,
            (namespace, sorted(TERMINAL_STREAM_STATUSES)),
        ),
        compacted_streaming_edit_archive_rows=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_compacted_streaming_edits
            WHERE namespace = %s
            """,
            (namespace,),
        ),
        compacted_streaming_edit_archive_bytes=await _count(
            db,
            """
            SELECT COALESCE(SUM(octet_length(event_json_zlib)), 0)
            FROM mindroom_event_cache_compacted_streaming_edits
            WHERE namespace = %s
            """,
            (namespace,),
        ),
        orphan_edit_indexes_before=orphan_counts_before[0],
        orphan_edit_indexes_after=await _orphan_edit_index_count(db, namespace=namespace),
        orphan_thread_indexes_before=orphan_counts_before[1],
        orphan_thread_indexes_after=await _orphan_thread_index_count(db, namespace=namespace),
        orphan_thread_event_references_before=orphan_counts_before[2],
        orphan_thread_event_references_after=await _orphan_thread_event_reference_count(db, namespace=namespace),
        repaired_edit_indexes=repaired_counts[0],
        repaired_thread_indexes=repaired_counts[1],
        repaired_thread_event_references=repaired_counts[2],
        compacted_nonterminal_streaming_edits=compacted_nonterminal_streaming_edits,
    )


async def run_startup_maintenance(
    db: AsyncConnection,
    *,
    namespace: str,
    schema_version: int,
    migrated_from_schema_version: int | None,
) -> CacheMaintenanceReport:
    """Audit, safely repair, compact, and recount one PostgreSQL namespace."""
    orphan_counts = (
        await _orphan_edit_index_count(db, namespace=namespace),
        await _orphan_thread_index_count(db, namespace=namespace),
        await _orphan_thread_event_reference_count(db, namespace=namespace),
    )
    repaired_counts = await _repair_orphan_derived_rows(db, namespace=namespace)
    compacted = await compact_superseded_streaming_edits(db, namespace=namespace)
    return await _collect_maintenance_report(
        db,
        namespace=namespace,
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        orphan_counts_before=orphan_counts,
        repaired_counts=repaired_counts,
        compacted_nonterminal_streaming_edits=compacted,
    )
