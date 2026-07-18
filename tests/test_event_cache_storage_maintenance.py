"""Storage migration, integrity repair, and operability tests for Matrix event caches."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

import aiosqlite
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mindroom.matrix.cache import (
    postgres_event_cache,
    postgres_event_cache_events,
    postgres_streaming_compaction,
    sqlite_event_cache,
)
from mindroom.matrix.cache.postgres_cache_maintenance import migrate_postgres_schema
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_ROOM_ID = "!room:localhost"
_THREAD_ID = "$root:localhost"
_CHILD_ID = "$child:localhost"
_MISSING_ID = "$missing:localhost"
_ORPHAN_ID = "$orphan:localhost"
_FUTURE_INVALIDATED_AT = 4_000_000_000.0


def _message_event(event_id: str, *, thread_id: str | None = None) -> dict[str, object]:
    content: dict[str, object] = {"msgtype": "m.text", "body": event_id}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 10,
        "content": content,
    }


def _streaming_edit(
    event_id: str,
    *,
    original_event_id: str,
    timestamp: int,
    status: str,
) -> dict[str, object]:
    """Build one Matrix streaming replacement event."""
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@agent:localhost",
        "origin_server_ts": timestamp,
        "content": {
            "msgtype": "m.text",
            "body": event_id,
            "m.new_content": {
                "msgtype": "m.text",
                "body": event_id,
                "io.mindroom.stream_status": status,
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
        },
    }


async def _prepare_sqlite_version_10(db_path: Path) -> None:
    db = await aiosqlite.connect(db_path)
    try:
        await sqlite_event_cache._create_event_cache_schema(db)
        await db.execute("DROP TABLE compacted_streaming_edits")
        await db.execute("DROP TABLE cache_metadata")
        await db.execute("DROP TABLE thread_events")
        await db.execute("DROP TABLE events")
        await db.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                origin_server_ts INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                cached_at REAL NOT NULL
            )
            """,
        )
        await db.execute(
            """
            CREATE INDEX idx_events_room_origin_ts
            ON events(room_id, origin_server_ts DESC)
            """,
        )
        await db.execute(
            """
            CREATE TABLE thread_events (
                room_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                origin_server_ts INTEGER NOT NULL,
                event_json TEXT NOT NULL,
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
        child_json = json.dumps(_message_event(_CHILD_ID, thread_id=_THREAD_ID))
        missing_json = json.dumps(_message_event(_MISSING_ID, thread_id=_THREAD_ID))
        await db.execute(
            """
            INSERT INTO events(event_id, room_id, origin_server_ts, event_json, cached_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_CHILD_ID, _ROOM_ID, 10, child_json, 1.0),
        )
        await db.executemany(
            """
            INSERT INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (_ROOM_ID, _THREAD_ID, _CHILD_ID, 10, child_json),
                (_ROOM_ID, _THREAD_ID, _MISSING_ID, 11, missing_json),
            ],
        )
        await db.executemany(
            """
            INSERT INTO event_threads(room_id, event_id, thread_id)
            VALUES (?, ?, ?)
            """,
            [
                (_ROOM_ID, _THREAD_ID, _THREAD_ID),
                (_ROOM_ID, _ORPHAN_ID, "$unlearned:localhost"),
            ],
        )
        await db.execute(
            """
            INSERT INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (?, ?, ?, ?)
            """,
            (_ORPHAN_ID, _ROOM_ID, _THREAD_ID, 12),
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
            VALUES (?, ?, NULL, ?, 'preexisting_newer_invalidation')
            """,
            (_ROOM_ID, _THREAD_ID, _FUTURE_INVALIDATED_AT),
        )
        await db.execute("PRAGMA user_version = 10")
        await db.commit()
    finally:
        await db.close()


@asynccontextmanager
async def _isolated_postgres_database(base_url: str) -> AsyncIterator[str]:
    database_name = f"mindroom_cache_{uuid.uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        yield make_conninfo(base_url, dbname=database_name)
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name)),
        )
        await admin.close()


async def _prepare_postgres_version_1(database_url: str, *, namespace: str, other_namespace: str) -> None:
    db = await psycopg.AsyncConnection.connect(database_url)
    try:
        await postgres_event_cache._create_postgres_event_cache_schema(db)
        await db.execute(
            """
            DROP TRIGGER mindroom_event_cache_rehydrate_compacted_event_trigger
            ON mindroom_event_cache_events
            """,
        )
        await db.execute(
            """
            DROP TRIGGER mindroom_event_cache_delete_redacted_compacted_events_trigger
            ON mindroom_event_cache_redacted_events
            """,
        )
        await db.execute("DROP FUNCTION mindroom_event_cache_rehydrate_compacted_event()")
        await db.execute("DROP FUNCTION mindroom_event_cache_delete_redacted_compacted_events()")
        await db.execute("DROP TABLE mindroom_event_cache_compacted_streaming_edits")
        await db.execute(
            """
            ALTER TABLE mindroom_event_cache_thread_events
            ALTER COLUMN event_json SET NOT NULL
            """,
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_metadata(key, value)
            VALUES ('schema_version', '1')
            """,
        )
        child_json = json.dumps(_message_event(_CHILD_ID, thread_id=_THREAD_ID))
        missing_json = json.dumps(_message_event(_MISSING_ID, thread_id=_THREAD_ID))
        for row_namespace in (namespace, other_namespace):
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_events(
                    namespace,
                    event_id,
                    room_id,
                    origin_server_ts,
                    event_json,
                    cached_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (row_namespace, _CHILD_ID, _ROOM_ID, 10, child_json, 1.0),
            )
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_thread_events(
                    namespace,
                    room_id,
                    thread_id,
                    event_id,
                    origin_server_ts,
                    event_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (row_namespace, _ROOM_ID, _THREAD_ID, _CHILD_ID, 10, child_json),
            )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_thread_events(
                namespace,
                room_id,
                thread_id,
                event_id,
                origin_server_ts,
                event_json
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (namespace, _ROOM_ID, _THREAD_ID, _MISSING_ID, 11, missing_json),
        )
        for event_id, thread_id in (
            (_THREAD_ID, _THREAD_ID),
            (_ORPHAN_ID, "$unlearned:localhost"),
        ):
            await db.execute(
                """
                INSERT INTO mindroom_event_cache_event_threads(namespace, room_id, event_id, thread_id)
                VALUES (%s, %s, %s, %s)
                """,
                (namespace, _ROOM_ID, event_id, thread_id),
            )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_event_edits(
                namespace,
                edit_event_id,
                room_id,
                original_event_id,
                origin_server_ts
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (namespace, _ORPHAN_ID, _ROOM_ID, _THREAD_ID, 12),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_version_10_migrates_without_reset_and_repairs_orphans(tmp_path: Path) -> None:
    """The exact version-10 shape migrates transactionally and preserves valid learned roots."""
    db_path = tmp_path / "event_cache.db"
    await _prepare_sqlite_version_10(db_path)

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        diagnostics = cache.runtime_diagnostics()
        cached_thread = await cache.get_thread_events(_ROOM_ID, _THREAD_ID)
        stale_state = await cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)

        assert cache.startup_requires_sync_reset is False
        assert diagnostics["cache_schema_migrated_from"] == 10
        assert diagnostics["cache_orphan_edit_indexes_before"] == 1
        assert diagnostics["cache_orphan_edit_indexes_after"] == 0
        assert diagnostics["cache_orphan_thread_indexes_before"] == 1
        assert diagnostics["cache_orphan_thread_indexes_after"] == 0
        assert cached_thread == [_message_event(_CHILD_ID, thread_id=_THREAD_ID)]
        assert stale_state is not None
        assert stale_state.invalidated_at == _FUTURE_INVALIDATED_AT
        assert stale_state.invalidation_reason == "preexisting_newer_invalidation"
        assert await cache.get_thread_id_for_event(_ROOM_ID, _THREAD_ID) == _THREAD_ID
        assert await cache.get_thread_id_for_event(_ROOM_ID, _ORPHAN_ID) is None

        cursor = await cache._runtime.require_db().execute("PRAGMA table_info(thread_events)")
        columns = [str(row[1]) for row in await cursor.fetchall()]
        await cursor.close()
        assert "event_json" not in columns
    finally:
        await cache.close()

    db = await aiosqlite.connect(db_path)
    try:
        await db.execute(
            """
            INSERT INTO thread_events(room_id, thread_id, event_id, origin_server_ts, write_seq)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_ROOM_ID, "$dangling-thread:localhost", "$dangling:localhost", 20, 100),
        )
        await db.commit()
    finally:
        await db.close()

    repaired_cache = SqliteEventCache(db_path)
    await repaired_cache.initialize()
    try:
        diagnostics = repaired_cache.runtime_diagnostics()
        assert diagnostics["cache_orphan_thread_event_references_before"] == 1
        assert diagnostics["cache_orphan_thread_event_references_after"] == 0
        assert diagnostics["cache_repaired_thread_event_references"] == 1
        state = await repaired_cache.get_thread_cache_state(_ROOM_ID, "$dangling-thread:localhost")
        assert state is not None
        assert state.invalidation_reason == "startup_orphan_thread_event_reference"
    finally:
        await repaired_cache.close()


@pytest.mark.asyncio
async def test_sqlite_version_10_migration_rolls_back_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after the schema rewrite leaves the complete version-10 database intact."""
    db_path = tmp_path / "event_cache.db"
    await _prepare_sqlite_version_10(db_path)
    cancel_reason = "migration cancelled"

    async def cancel_maintenance(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(sqlite_event_cache, "run_startup_maintenance", cancel_maintenance)
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await sqlite_event_cache._initialize_event_cache_db(db_path)

    db = await aiosqlite.connect(db_path)
    try:
        version_cursor = await db.execute("PRAGMA user_version")
        assert await version_cursor.fetchone() == (10,)
        await version_cursor.close()
        column_cursor = await db.execute("PRAGMA table_info(thread_events)")
        columns = [str(row[1]) for row in await column_cursor.fetchall()]
        await column_cursor.close()
        archive_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name = 'compacted_streaming_edits'
            """,
        )
        assert await archive_cursor.fetchone() == (0,)
        await archive_cursor.close()
        assert "event_json" in columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_unsupported_schema_reset_requires_sync_recertification(tmp_path: Path) -> None:
    """Only an unsupported destructive reset requests a cold sync-token restart."""
    db_path = tmp_path / "event_cache.db"
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("CREATE TABLE events(old_payload TEXT)")
        await db.execute("PRAGMA user_version = 9")
        await db.commit()
    finally:
        await db.close()

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        diagnostics = cache.runtime_diagnostics()
        assert cache.startup_requires_sync_reset is True
        assert diagnostics["cache_schema_destructive_reset"] is True
        assert diagnostics["cache_event_rows"] == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_version_1_migration_is_namespace_safe_and_repairs_orphans(
    postgres_event_cache_url: str,
) -> None:
    """PostgreSQL migration normalizes only the initializing namespace and reports repairs."""
    namespace = f"tenant_{uuid.uuid4().hex}"
    other_namespace = f"tenant_{uuid.uuid4().hex}"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        await _prepare_postgres_version_1(
            database_url,
            namespace=namespace,
            other_namespace=other_namespace,
        )
        cache = PostgresEventCache(database_url=database_url, namespace=namespace)
        await cache.initialize()
        try:
            diagnostics = cache.runtime_diagnostics()
            cached_thread = await cache.get_thread_events(_ROOM_ID, _THREAD_ID)
            stale_state = await cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)

            assert diagnostics["cache_schema_migrated_from"] == 1
            assert diagnostics["cache_orphan_edit_indexes_before"] == 1
            assert diagnostics["cache_orphan_edit_indexes_after"] == 0
            assert diagnostics["cache_orphan_thread_indexes_before"] == 1
            assert diagnostics["cache_orphan_thread_indexes_after"] == 0
            assert diagnostics["cache_storage_bytes"] > 0
            assert diagnostics["cache_namespace_payload_bytes"] > 0
            assert cached_thread == [_message_event(_CHILD_ID, thread_id=_THREAD_ID)]
            assert stale_state is not None
            assert stale_state.invalidation_reason == "startup_orphan_thread_event_reference"
            assert await cache.get_thread_id_for_event(_ROOM_ID, _THREAD_ID) == _THREAD_ID

            db = cache._runtime.require_db()
            cursor = await db.execute(
                """
                SELECT namespace, event_json
                FROM mindroom_event_cache_thread_events
                WHERE event_id = %s
                ORDER BY namespace
                """,
                (_CHILD_ID,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            assert rows == sorted(
                [(namespace, None), (other_namespace, json.dumps(_message_event(_CHILD_ID, thread_id=_THREAD_ID)))],
            )
        finally:
            await cache.close()

        other_cache = PostgresEventCache(database_url=database_url, namespace=other_namespace)
        await other_cache.initialize()
        try:
            cursor = await other_cache._runtime.require_db().execute(
                """
                SELECT event_json
                FROM mindroom_event_cache_thread_events
                WHERE namespace = %s AND event_id = %s
                """,
                (other_namespace, _CHILD_ID),
            )
            assert await cursor.fetchone() == (None,)
            await cursor.close()
        finally:
            await other_cache.close()


@pytest.mark.asyncio
async def test_postgres_version_2_maintenance_avoids_exclusive_schema_lock(
    postgres_event_cache_url: str,
) -> None:
    """Routine namespace maintenance must run beside readers without repeating migration DDL."""
    namespace = f"tenant_{uuid.uuid4().hex}"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        cache = PostgresEventCache(database_url=database_url, namespace=namespace)
        await cache.initialize()
        await cache.close()

        blocker = await psycopg.AsyncConnection.connect(database_url)
        maintainer = await psycopg.AsyncConnection.connect(database_url)
        try:
            await blocker.execute(
                "LOCK TABLE mindroom_event_cache_thread_events IN ACCESS SHARE MODE",
            )
            await maintainer.execute("SET statement_timeout = '500ms'")
            migrated_from = await migrate_postgres_schema(
                maintainer,
                namespace=namespace,
                current_schema_version=2,
                target_schema_version=2,
            )
            assert migrated_from is None
            await maintainer.rollback()
        finally:
            await blocker.rollback()
            await blocker.close()
            await maintainer.close()


@pytest.mark.asyncio
async def test_postgres_startup_compaction_rechecks_after_locked_redaction(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup compaction cannot archive a payload redacted by an overlapping old runtime."""
    namespace = f"tenant_{uuid.uuid4().hex}"
    pending_id = "$pending:localhost"
    original_id = "$original:localhost"
    pending = _streaming_edit(
        pending_id,
        original_event_id=original_id,
        timestamp=2,
        status="pending",
    )
    terminal = _streaming_edit(
        "$terminal:localhost",
        original_event_id=original_id,
        timestamp=3,
        status="completed",
    )
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        cache = PostgresEventCache(database_url=database_url, namespace=namespace)
        await cache.initialize()
        monkeypatch.setattr(
            postgres_event_cache_events,
            "compact_superseded_streaming_edits",
            _no_postgres_compaction,
        )
        try:
            await cache.store_events_batch(
                [
                    (pending_id, _ROOM_ID, pending),
                    (str(terminal["event_id"]), _ROOM_ID, terminal),
                ],
            )
        finally:
            await cache.close()

        maintainer = await psycopg.AsyncConnection.connect(database_url)
        redactor = await psycopg.AsyncConnection.connect(database_url)
        discovered = asyncio.Event()
        resume_compaction = asyncio.Event()
        real_candidates = postgres_streaming_compaction._compaction_candidates
        compaction_task: asyncio.Task[int] | None = None

        async def signal_discovery(
            db: psycopg.AsyncConnection,
            *,
            namespace: str,
            room_id: str | None,
            limit: int,
        ) -> list[postgres_streaming_compaction._ArchivedPostgresStreamingEdit]:
            candidates = await real_candidates(
                db,
                namespace=namespace,
                room_id=room_id,
                limit=limit,
            )
            if room_id is None and candidates:
                discovered.set()
                await resume_compaction.wait()
            return candidates

        monkeypatch.setattr(postgres_streaming_compaction, "_compaction_candidates", signal_discovery)
        try:
            await redactor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (namespace, _ROOM_ID),
            )
            compaction_task = asyncio.create_task(
                postgres_streaming_compaction.compact_superseded_streaming_edits(
                    maintainer,
                    namespace=namespace,
                ),
            )
            await asyncio.wait_for(discovered.wait(), timeout=5)
            assert compaction_task.done() is False

            assert await postgres_event_cache_events.redact_event_locked(
                redactor,
                namespace=namespace,
                room_id=_ROOM_ID,
                event_id=pending_id,
            )
            await redactor.commit()
            resume_compaction.set()
            assert await asyncio.wait_for(compaction_task, timeout=5) == 0
            await maintainer.commit()

            assert await _postgres_archive_and_tombstone_state(
                maintainer,
                namespace=namespace,
                event_id=pending_id,
            ) == (False, True)
        finally:
            resume_compaction.set()
            if compaction_task is not None and not compaction_task.done():
                compaction_task.cancel()
                with suppress(asyncio.CancelledError):
                    await compaction_task
            await maintainer.rollback()
            await redactor.rollback()
            await maintainer.close()
            await redactor.close()


@pytest.mark.asyncio
async def test_postgres_cutover_triggers_coordinate_surviving_version_1_runtime(
    postgres_event_cache_url: str,
) -> None:
    """Version-1 writes after migration cannot duplicate or resurrect version-2 cold rows."""
    namespace = f"tenant_{uuid.uuid4().hex}"
    replayed_pending_id = "$replayed-pending:localhost"
    replayed_original_id = "$replayed-original:localhost"
    replayed_pending = _streaming_edit(
        replayed_pending_id,
        original_event_id=replayed_original_id,
        timestamp=2,
        status="pending",
    )
    replayed_terminal = _streaming_edit(
        "$replayed-terminal:localhost",
        original_event_id=replayed_original_id,
        timestamp=3,
        status="completed",
    )
    redacted_pending_id = "$redacted-pending:localhost"
    redacted_original_id = "$redacted-original:localhost"
    redacted_pending = _streaming_edit(
        redacted_pending_id,
        original_event_id=redacted_original_id,
        timestamp=4,
        status="pending",
    )
    redacted_terminal = _streaming_edit(
        "$redacted-terminal:localhost",
        original_event_id=redacted_original_id,
        timestamp=5,
        status="completed",
    )
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        cache = PostgresEventCache(database_url=database_url, namespace=namespace)
        await cache.initialize()
        try:
            await replace_thread_unconditionally(
                cache,
                _ROOM_ID,
                replayed_original_id,
                [replayed_pending, replayed_terminal],
            )
            await cache.store_events_batch(
                [
                    (redacted_pending_id, _ROOM_ID, redacted_pending),
                    (str(redacted_terminal["event_id"]), _ROOM_ID, redacted_terminal),
                ],
            )

            old_runtime = await psycopg.AsyncConnection.connect(database_url)
            try:
                await old_runtime.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (namespace, _ROOM_ID),
                )
                await old_runtime.execute(
                    """
                    INSERT INTO mindroom_event_cache_events(
                        namespace,
                        event_id,
                        room_id,
                        origin_server_ts,
                        event_json,
                        cached_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(namespace, event_id) DO UPDATE SET
                        event_json = excluded.event_json,
                        write_seq = nextval('mindroom_event_cache_write_seq')
                    """,
                    (
                        namespace,
                        replayed_pending_id,
                        _ROOM_ID,
                        replayed_pending["origin_server_ts"],
                        json.dumps(replayed_pending),
                        1.0,
                    ),
                )
                await old_runtime.execute(
                    """
                    INSERT INTO mindroom_event_cache_redacted_events(namespace, room_id, event_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(namespace, room_id, event_id) DO NOTHING
                    """,
                    (namespace, _ROOM_ID, redacted_original_id),
                )
                await old_runtime.commit()
            finally:
                await old_runtime.close()

            assert await cache.get_event(_ROOM_ID, replayed_pending_id) == replayed_pending
            replayed_thread = await cache.get_thread_events(_ROOM_ID, replayed_original_id)
            assert replayed_thread is not None
            assert [event["event_id"] for event in replayed_thread].count(replayed_pending_id) == 1
            assert await cache.get_event(_ROOM_ID, redacted_pending_id) is None

            db = cache._runtime.require_db()
            cursor = await db.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_compacted_streaming_edits
                        WHERE namespace = %s AND event_id = %s
                    ),
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_event_edits
                        WHERE namespace = %s AND edit_event_id = %s
                    ),
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_thread_events
                        WHERE namespace = %s AND event_id = %s
                    ),
                    EXISTS (
                        SELECT 1
                        FROM mindroom_event_cache_compacted_streaming_edits
                        WHERE namespace = %s AND event_id = %s
                    )
                """,
                (
                    namespace,
                    replayed_pending_id,
                    namespace,
                    replayed_pending_id,
                    namespace,
                    replayed_pending_id,
                    namespace,
                    redacted_pending_id,
                ),
            )
            assert await cursor.fetchone() == (False, True, True, False)
            await cursor.close()
        finally:
            await cache.close()


async def _no_postgres_compaction(*_args: object, **_kwargs: object) -> int:
    """Leave active candidates in place for a startup-compaction race test."""
    return 0


async def _postgres_archive_and_tombstone_state(
    db: psycopg.AsyncConnection,
    *,
    namespace: str,
    event_id: str,
) -> tuple[bool, bool]:
    """Return whether one event has a cold payload and a durable tombstone."""
    row = await db.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM mindroom_event_cache_compacted_streaming_edits
                WHERE namespace = %s AND event_id = %s
            ),
            EXISTS (
                SELECT 1
                FROM mindroom_event_cache_redacted_events
                WHERE namespace = %s AND event_id = %s
            )
        """,
        (namespace, event_id, namespace, event_id),
    )
    result = await row.fetchone()
    await row.close()
    assert result is not None
    return bool(result[0]), bool(result[1])


@pytest.mark.asyncio
async def test_postgres_version_1_migration_rolls_back_on_cancellation(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after PostgreSQL DDL and namespace updates rolls the whole migration back."""
    namespace = f"tenant_{uuid.uuid4().hex}"
    other_namespace = f"tenant_{uuid.uuid4().hex}"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        await _prepare_postgres_version_1(
            database_url,
            namespace=namespace,
            other_namespace=other_namespace,
        )
        cancel_reason = "migration cancelled"

        async def cancel_maintenance(*_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError(cancel_reason)

        monkeypatch.setattr(postgres_event_cache, "run_startup_maintenance", cancel_maintenance)
        with pytest.raises(asyncio.CancelledError, match=cancel_reason):
            await postgres_event_cache._initialize_postgres_event_cache_db(
                database_url,
                namespace=namespace,
            )

        db = await psycopg.AsyncConnection.connect(database_url)
        try:
            version_cursor = await db.execute(
                """
                SELECT value
                FROM mindroom_event_cache_metadata
                WHERE key = 'schema_version'
                """,
            )
            assert await version_cursor.fetchone() == ("1",)
            await version_cursor.close()
            payload_cursor = await db.execute(
                """
                SELECT event_json IS NOT NULL
                FROM mindroom_event_cache_thread_events
                WHERE namespace = %s AND event_id = %s
                """,
                (namespace, _CHILD_ID),
            )
            assert await payload_cursor.fetchone() == (True,)
            await payload_cursor.close()
            archive_cursor = await db.execute(
                "SELECT to_regclass('mindroom_event_cache_compacted_streaming_edits')",
            )
            assert await archive_cursor.fetchone() == (None,)
            await archive_cursor.close()
        finally:
            await db.close()
