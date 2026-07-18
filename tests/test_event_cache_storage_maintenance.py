"""Storage migration, integrity repair, and operability tests for Matrix event caches."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mindroom.matrix.cache import postgres_event_cache, sqlite_event_cache
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_ROOM_ID = "!room:localhost"
_THREAD_ID = "$root:localhost"
_CHILD_ID = "$child:localhost"
_MISSING_ID = "$missing:localhost"
_ORPHAN_ID = "$orphan:localhost"


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


async def _prepare_sqlite_version_10(db_path: Path) -> None:
    db = await aiosqlite.connect(db_path)
    try:
        await sqlite_event_cache._create_event_cache_schema(db)
        await db.execute("DROP TABLE compacted_streaming_edits")
        await db.execute("DROP TABLE thread_events")
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
                (_ROOM_ID, _CHILD_ID, _THREAD_ID),
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
            (_CHILD_ID, _THREAD_ID),
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
        assert stale_state.invalidation_reason == "schema_migration_missing_thread_event_source"
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
            INSERT INTO thread_events(room_id, thread_id, event_id, origin_server_ts)
            VALUES (?, ?, ?, ?)
            """,
            (_ROOM_ID, "$dangling-thread:localhost", "$dangling:localhost", 20),
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
