"""PostgreSQL schema migration, integrity repair, and diagnostics."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cache_maintenance import CacheMaintenanceReport
from .postgres_cursor import fetchone, rowcount
from .postgres_event_cache_events import orphan_thread_index_count, repair_orphan_thread_indexes

if TYPE_CHECKING:
    from typing import LiteralString

    from psycopg import AsyncConnection


@dataclass(frozen=True, slots=True)
class _PostgresSchemaMigrationResult:
    """Namespace normalization outcome inside the shared schema transaction."""

    migrated_from_schema_version: int | None
    normalized_legacy_thread_payload_rows: int


_SENDER_BACKFILL_MARKER_KEY = "sender_backfilled"

# The six-character JSON escape for NUL, spelled without embedding a NUL in this file.
_JSON_NUL_ESCAPE = "\\u0000"


async def _backfill_collapsed_read_columns(db: AsyncConnection, *, namespace: str) -> None:
    r"""Populate the sender column a collapsed read needs on one namespace's pre-existing rows.

    Gated on a per-namespace marker rather than on ``schema_version``. That version lives in
    ``mindroom_event_cache_metadata``, which is keyed by ``key`` alone and is therefore global to
    the database, while every Matrix principal owns its own namespace and initializes separately.
    Gating on the shared version would let the first principal to start backfill its own rows,
    write the new version, and leave every other principal's rows behind forever.

    Running unconditionally instead is not free: no index covers ``sender``, so it is a heap scan
    of every row in the namespace on every initialization, inside the advisory-lock transaction
    that serializes other principals' startup. Budget roughly 1.4 s per 50,000 rows for the read
    half alone, measured against the production table (122,555 rows, 538 MB, ~4.4 KB per row) where
    the ``jsonb`` cast has to detoast and parse each payload; an earlier figure of 13.7 ms per
    50,000 rows came from a synthetic table of tiny payloads and understated real data by about
    two orders of magnitude. It also rewrites every row whose payload genuinely carries no sender,
    forever, because such a row can never leave the '' default.

    Rows whose payload contains a ``\u0000`` escape are skipped, because ``jsonb`` cannot
    represent one and the cast raises ``UntranslatableCharacter`` for the whole statement. Since
    ``event_json`` is always written by ``json.dumps`` it is always syntactically valid JSON, and
    that escape is the only thing valid JSON may carry that ``jsonb`` rejects - so this predicate
    is complete, not merely sufficient for the payloads seen so far. Without it the backfill
    aborts the migration transaction, which is re-raised by ``_initialize_postgres_event_cache_db``,
    so a single such row stops that whole namespace from ever initializing. Production held 47 of
    them across two namespaces, from tool output that captured binary content into a message body.
    Sanitizing rather than skipping was tried and rejected: replacing the escape corrupts
    neighbouring backslash runs and turns valid payloads into invalid ones.

    The marker costs the self-healing property an unconditional backfill would have, which is
    acceptable here rather than merely convenient: a row can only reach '' after the marker if a
    writer omits the sender, and the sole INSERT supplies it from ``SerializedCachedEvent.sender``.
    That yields '' only for a payload with no string sender - exactly the rows the backfill
    expression cannot recover either. Supported deployments are single-replica ``Recreate``, so
    there is no mixed-version writer, and an older build refuses to start against schema 4 rather
    than writing behind the marker.

    A ``sender`` at its '' default makes every event look like it came from the same account, so a
    collapsed read can no longer tell an author's own edit from someone else's. The message then
    renders at its pre-edit body - the fold refuses the foreign edit, so this is a wrong body
    rather than an impersonation. That is also the bounded cost of the skip above: a skipped row
    keeps '', so an edit of it renders un-edited rather than wrong-authored.
    """
    marked = await fetchone(
        db,
        "SELECT 1 FROM mindroom_event_cache_namespace_metadata WHERE namespace = %s AND key = %s",
        (namespace, _SENDER_BACKFILL_MARKER_KEY),
    )
    if marked is not None:
        return
    await db.execute(
        """
        UPDATE mindroom_event_cache_events
        SET sender = COALESCE(event_json::jsonb ->> 'sender', '')
        WHERE namespace = %s AND sender = '' AND strpos(event_json, %s) = 0
        """,
        (namespace, _JSON_NUL_ESCAPE),
    )
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_namespace_metadata(namespace, key, value)
        VALUES (%s, %s, 'done')
        ON CONFLICT(namespace, key) DO NOTHING
        """,
        (namespace, _SENDER_BACKFILL_MARKER_KEY),
    )


async def migrate_postgres_schema(
    db: AsyncConnection,
    *,
    namespace: str,
    current_schema_version: int | None,
    target_schema_version: int,
) -> _PostgresSchemaMigrationResult:
    """Transactionally normalize one namespace while upgrading the shared schema."""
    if current_schema_version not in {None, 1, 2, 3, target_schema_version}:
        msg = (
            "PostgreSQL Matrix event cache schema version "
            f"{current_schema_version} is not compatible with expected version {target_schema_version}"
        )
        raise RuntimeError(msg)

    upgrading = current_schema_version is not None and current_schema_version < target_schema_version
    migrated_from = current_schema_version if upgrading else None
    if current_schema_version == 1:
        await db.execute(
            """
            ALTER TABLE mindroom_event_cache_thread_events
            ALTER COLUMN event_json DROP NOT NULL
            """,
        )
    if upgrading:
        # Only while upgrading. ALTER TABLE takes ACCESS EXCLUSIVE even when IF NOT EXISTS makes it
        # a no-op, so running it unconditionally would briefly lock the events table on every
        # startup, against the one connection every other principal is waiting on. A database
        # created at the current version already has the column from CREATE TABLE.
        await db.execute(
            """
            ALTER TABLE mindroom_event_cache_events
            ADD COLUMN IF NOT EXISTS sender TEXT NOT NULL DEFAULT ''
            """,
        )
    await _backfill_collapsed_read_columns(db, namespace=namespace)

    normalized_legacy_thread_payload_rows = await rowcount(
        db,
        """
        UPDATE mindroom_event_cache_thread_events
        SET event_json = NULL
        WHERE namespace = %s AND event_json IS NOT NULL
        """,
        (namespace,),
    )
    if normalized_legacy_thread_payload_rows:
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
                'schema_migration_missing_thread_event_source'
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
                validated_at = NULL,
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
            (time.time(), namespace),
        )
        await db.execute(
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

    await db.execute(
        """
        INSERT INTO mindroom_event_cache_metadata(key, value)
        VALUES ('schema_version', %s)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(target_schema_version),),
    )
    return _PostgresSchemaMigrationResult(
        migrated_from_schema_version=migrated_from,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
    )


async def _count(
    db: AsyncConnection,
    query: LiteralString,
    parameters: tuple[object, ...],
) -> int:
    row = await fetchone(db, query, parameters)
    return 0 if row is None else int(row[0])


_ORPHAN_EDIT_INDEX_PREDICATE = """
    NOT EXISTS (
        SELECT 1
        FROM mindroom_event_cache_events AS events
        WHERE events.namespace = event_edits.namespace
            AND events.event_id = event_edits.edit_event_id
            AND events.room_id = event_edits.room_id
    )
"""


async def _orphan_edit_index_count(db: AsyncConnection, *, namespace: str) -> int:
    return await _count(
        db,
        f"""
        SELECT COUNT(*)
        FROM mindroom_event_cache_event_edits AS event_edits
        WHERE event_edits.namespace = %s
            AND {_ORPHAN_EDIT_INDEX_PREDICATE}
        """,  # noqa: S608
        (namespace,),
    )


async def _repair_orphan_derived_rows(
    db: AsyncConnection,
    *,
    namespace: str,
) -> tuple[int, int]:
    """Remove invalid derived rows while preserving learned thread-root mappings."""
    repaired_edit_indexes = await rowcount(
        db,
        f"""
        DELETE FROM mindroom_event_cache_event_edits AS event_edits
        WHERE event_edits.namespace = %s
            AND {_ORPHAN_EDIT_INDEX_PREDICATE}
        """,  # noqa: S608
        (namespace,),
    )
    repaired_thread_indexes = await repair_orphan_thread_indexes(db, namespace=namespace)
    return repaired_edit_indexes, repaired_thread_indexes


async def _collect_maintenance_report(
    db: AsyncConnection,
    *,
    namespace: str,
    schema_version: int,
    migrated_from_schema_version: int | None,
    normalized_legacy_thread_payload_rows: int,
    repaired_counts: tuple[int, int],
) -> CacheMaintenanceReport:
    """Collect log-safe backend and namespace storage diagnostics."""
    return CacheMaintenanceReport(
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        storage_bytes=await _count(db, "SELECT pg_database_size(current_database())", ()),
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
        orphan_edit_indexes_after=await _orphan_edit_index_count(db, namespace=namespace),
        orphan_thread_indexes_after=await orphan_thread_index_count(db, namespace=namespace),
        repaired_edit_indexes=repaired_counts[0],
        repaired_thread_indexes=repaired_counts[1],
    )


async def run_startup_maintenance(
    db: AsyncConnection,
    *,
    namespace: str,
    schema_version: int,
    migrated_from_schema_version: int | None,
    normalized_legacy_thread_payload_rows: int,
) -> CacheMaintenanceReport:
    """Audit, safely repair, and recount one PostgreSQL namespace."""
    repaired_counts = await _repair_orphan_derived_rows(db, namespace=namespace)
    return await _collect_maintenance_report(
        db,
        namespace=namespace,
        schema_version=schema_version,
        migrated_from_schema_version=migrated_from_schema_version,
        normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        repaired_counts=repaired_counts,
    )
