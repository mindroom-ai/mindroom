"""PostgreSQL schema migration, integrity repair, and diagnostics."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cache_maintenance import CacheMaintenanceReport
from .postgres_cursor import fetchall, fetchone, rowcount
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

# Keyset page size for the backfill. Small enough that one page of payloads is a bounded amount of
# memory (cached payloads commonly run to several kilobytes), large enough that round trips
# do not dominate.
_SENDER_BACKFILL_PAGE_ROWS = 500


def _sender_from_cached_payload(event_json: str) -> str | None:
    """Return the storable sender from one cached payload, or None when there is nothing to write."""
    sender = json.loads(event_json).get("sender")
    if not isinstance(sender, str) or not sender:
        return None
    # PostgreSQL text cannot hold a NUL, and a sender carrying one is malformed anyway.
    return None if "\x00" in sender else sender


async def _backfill_collapsed_read_columns(db: AsyncConnection, *, namespace: str) -> None:
    """Populate the sender column a collapsed read needs on one namespace's pre-existing rows.

    Gated on a per-namespace marker rather than on ``schema_version``. That version lives in
    ``mindroom_event_cache_metadata``, which is keyed by ``key`` alone and is therefore global to
    the database, while every Matrix principal owns its own namespace and initializes separately.
    Gating on the shared version would let the first principal to start backfill its own rows,
    write the new version, and leave every other principal's rows behind forever.

    The sender is decoded in Python rather than with ``event_json::jsonb ->> 'sender'`` in SQL,
    because that cast cannot process every payload this cache legitimately stores and one row it
    rejects aborts the whole statement. The migration transaction then fails and
    ``_initialize_postgres_event_cache_db`` re-raises, so a single bad row locks that namespace out
    of its cache on every restart, permanently. PostgreSQL rejects at least two shapes that
    ``json.dumps`` emits and ``json.loads`` reads back without complaint: a NUL escape
    (``UntranslatableCharacter``) and a lone surrogate (``InvalidTextRepresentation``). Both occur
    in real caches; the NUL escape turns up when tool output captures binary content into a
    message body.

    Two narrower SQL guards were tried and rejected. Rewriting the payload to strip the offending
    escape merges into neighbouring backslash runs and turns valid JSON invalid. Skipping rows by
    ``strpos`` over that escape is both unsound and incomplete: it also matches a payload that
    merely quotes the escape as literal text, which casts fine - and on a real cache most matches
    were exactly that - while it does not match a lone surrogate at all. Decoding with
    ``json.loads`` needs no such guard, because it is the exact inverse of the ``json.dumps``
    that wrote the row.

    Cost is one keyset-paginated pass over the namespace's un-backfilled rows, inside the
    advisory-lock transaction that serializes other principals' startup, once per namespace ever.
    Budget tens of seconds for a large namespace, because every payload has to cross the wire. An
    earlier revision of this docstring claimed 13.7 ms per 50,000 rows for a SQL-side cast; that
    came from a synthetic table of tiny payloads and understated realistic ones by about two
    orders of magnitude, because those live in TOAST and have to be detoasted and parsed.

    The marker costs the self-healing property an unconditional backfill would have, which is
    acceptable here rather than merely convenient: a row can only reach '' after the marker if a
    writer omits the sender, and the sole INSERT supplies it from ``SerializedCachedEvent.sender``.
    That yields '' only for a payload with no string sender - exactly the rows this pass also
    leaves alone. Supported deployments are single-replica ``Recreate``, so there is no
    mixed-version writer, and an older build refuses to start against schema 4 rather than writing
    behind the marker.

    A ``sender`` at its '' default makes every event look like it came from the same account, so a
    collapsed read can no longer tell an author's own edit from someone else's. The message then
    renders at its pre-edit body - the fold refuses the foreign edit, so this is a wrong body
    rather than an impersonation.
    """
    marked = await fetchone(
        db,
        "SELECT 1 FROM mindroom_event_cache_namespace_metadata WHERE namespace = %s AND key = %s",
        (namespace, _SENDER_BACKFILL_MARKER_KEY),
    )
    if marked is not None:
        return
    # Keyset pagination over the primary key. Rows this pass cannot resolve keep '' and so stay in
    # the predicate, which is why the cursor advances by key rather than by re-running the filter.
    last_room_id, last_event_id = "", ""
    while True:
        page = await fetchall(
            db,
            """
            SELECT room_id, event_id, event_json
            FROM mindroom_event_cache_events
            WHERE namespace = %s AND sender = '' AND (room_id, event_id) > (%s, %s)
            ORDER BY room_id, event_id
            LIMIT %s
            """,
            (namespace, last_room_id, last_event_id, _SENDER_BACKFILL_PAGE_ROWS),
        )
        if not page:
            break
        room_ids: list[str] = []
        event_ids: list[str] = []
        senders: list[str] = []
        for room_id, event_id, event_json in page:
            last_room_id, last_event_id = str(room_id), str(event_id)
            if (sender := _sender_from_cached_payload(str(event_json))) is None:
                continue
            room_ids.append(last_room_id)
            event_ids.append(last_event_id)
            senders.append(sender)
        if not senders:
            continue
        # One statement per page, not per row. Pagination bounds memory; this bounds round trips,
        # which is the cost that matters while the startup advisory lock is held.
        await db.execute(
            """
            UPDATE mindroom_event_cache_events AS events
            SET sender = backfilled.sender
            FROM unnest(%s::text[], %s::text[], %s::text[])
                AS backfilled(room_id, event_id, sender)
            WHERE events.namespace = %s
                AND events.room_id = backfilled.room_id
                AND events.event_id = backfilled.event_id
            """,
            (room_ids, event_ids, senders, namespace),
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
    if current_schema_version not in {None, 1, 2, 3, 4, target_schema_version}:
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
                gap_marked_at,
                gap_reason
            )
            SELECT DISTINCT
                thread_events.namespace,
                thread_events.room_id,
                thread_events.thread_id,
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
                gap_marked_at = GREATEST(
                    mindroom_event_cache_thread_state.gap_marked_at,
                    excluded.gap_marked_at
                ),
                gap_reason = CASE
                    WHEN mindroom_event_cache_thread_state.gap_marked_at IS NULL
                        OR excluded.gap_marked_at >= mindroom_event_cache_thread_state.gap_marked_at
                        THEN excluded.gap_reason
                    ELSE mindroom_event_cache_thread_state.gap_reason
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
            WHERE namespace = %s AND gap_marked_at IS NOT NULL
            """,
            (namespace,),
        ),
        # A room gap that is wrongly advanced puts every thread in that room into permanent
        # refetch, because each replacement copies the uncovered marker onto the snapshot it just
        # installed. Nothing else would show it: the per-thread count above cannot distinguish a
        # room fan-out from ordinary churn.
        room_gap_markers=await _count(
            db,
            """
            SELECT COUNT(*)
            FROM mindroom_event_cache_room_state
            WHERE namespace = %s AND room_gap_marked_at IS NOT NULL
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
