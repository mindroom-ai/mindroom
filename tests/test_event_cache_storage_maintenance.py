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

from mindroom.matrix.cache import (
    ThreadCacheState,
    postgres_event_cache,
    sqlite_cache_maintenance,
    sqlite_event_cache,
)
from mindroom.matrix.cache.postgres_cache_maintenance import (
    _SENDER_BACKFILL_PAGE_ROWS,
    _backfill_collapsed_read_columns,
    migrate_postgres_schema,
)
from mindroom.matrix.cache.postgres_event_cache import (
    _POSTGRES_EVENT_CACHE_SCHEMA_VERSION,
    PostgresEventCache,
)
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_ROOM_ID = "!room:localhost"
_THREAD_ID = "$root:localhost"
_CHILD_ID = "$child:localhost"
_MISSING_ID = "$missing:localhost"
_ORPHAN_ID = "$orphan:localhost"
_FUTURE_INVALIDATED_AT = 4_000_000_000.0
_FUTURE_VALIDATED_AT = 5_000_000_000.0


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


def _assert_missing_source_state(state: ThreadCacheState | None) -> None:
    assert state is not None
    assert state.validated_at is None
    assert state.invalidation_reason == "schema_migration_missing_thread_event_source"


async def _prepare_sqlite_version_10(db_path: Path) -> None:
    db = await aiosqlite.connect(db_path)
    try:
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
        child_json = json.dumps(_message_event(_CHILD_ID, thread_id=_THREAD_ID))
        await db.execute(
            """
            INSERT INTO events(event_id, room_id, origin_server_ts, event_json, cached_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_CHILD_ID, _ROOM_ID, 10, child_json, 1.0),
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
            ALTER TABLE mindroom_event_cache_thread_events
            ALTER COLUMN event_json SET NOT NULL
            """,
        )
        await db.execute(
            """
            CREATE TABLE mindroom_event_cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
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
        for row_namespace in (namespace, other_namespace):
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
                (row_namespace, _ROOM_ID, _THREAD_ID, _MISSING_ID, 11, missing_json),
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
            VALUES (%s, %s, %s, %s, %s, 'preexisting_newer_invalidation')
            """,
            (
                namespace,
                _ROOM_ID,
                _THREAD_ID,
                _FUTURE_VALIDATED_AT,
                _FUTURE_INVALIDATED_AT,
            ),
        )
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_unowned_version_10_is_reset(tmp_path: Path) -> None:
    """Rows without a principal owner are discarded instead of assigned speculatively."""
    db_path = tmp_path / "event_cache.db"
    await _prepare_sqlite_version_10(db_path)

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_schema_destructive_reset"] is True
        assert diagnostics["cache_event_rows"] == 0
        assert diagnostics["cache_storage_bytes"] > 0
        assert await cache.get_event(_ROOM_ID, _CHILD_ID) is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_sqlite_version_10_migration_rolls_back_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during reset leaves the version-10 database intact."""
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
        event_cursor = await db.execute("SELECT event_id FROM events")
        assert await event_cursor.fetchone() == (_CHILD_ID,)
        await event_cursor.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sqlite_unsupported_schema_reset_reports_destructive_reset(tmp_path: Path) -> None:
    """An unsupported schema is reset and reported without retaining old rows."""
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
        assert diagnostics["cache_schema_destructive_reset"] is True
        assert diagnostics["cache_event_rows"] == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_sqlite_startup_report_uses_nonblocking_read_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup diagnostics must stay coherent without blocking active cache writes."""
    db_path = tmp_path / "event_cache.db"
    primary = SqliteEventCache(db_path)
    await primary.initialize()
    await primary._runtime.require_db().execute("PRAGMA busy_timeout=0")
    report_started = asyncio.Event()
    release_report = asyncio.Event()
    scalar_count = sqlite_cache_maintenance._scalar_count

    async def held_first_count(
        db: aiosqlite.Connection,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> int:
        result = await scalar_count(db, query, parameters)
        if query == "SELECT COUNT(*) FROM events":
            report_started.set()
            await release_report.wait()
        return result

    monkeypatch.setattr(sqlite_cache_maintenance, "_scalar_count", held_first_count)
    secondary = SqliteEventCache(db_path)
    initialize_secondary = asyncio.create_task(secondary.initialize())
    try:
        await asyncio.wait_for(report_started.wait(), timeout=1)
        await primary.mark_thread_stale(_ROOM_ID, _THREAD_ID, reason="concurrent_startup_report")
    finally:
        release_report.set()
        await initialize_secondary
        diagnostics = secondary.runtime_diagnostics()
        await secondary.close()
        await primary.close()

    assert diagnostics["cache_event_rows"] == 0
    assert diagnostics["cache_thread_state_rows"] == 0
    assert diagnostics["cache_room_state_rows"] == 0
    assert diagnostics["cache_stale_thread_markers"] == 0


@pytest.mark.asyncio
async def test_postgres_version_1_migration_is_namespace_safe_and_repairs_orphans(
    postgres_event_cache_url: str,
) -> None:
    """PostgreSQL migration and trust reset affect only the initializing namespace."""
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
            assert diagnostics["cache_orphan_edit_indexes_after"] == 0
            assert diagnostics["cache_orphan_thread_indexes_after"] == 0
            assert diagnostics["cache_repaired_edit_indexes"] == 1
            assert diagnostics["cache_repaired_thread_indexes"] == 2
            assert diagnostics["cache_normalized_legacy_thread_payload_rows"] == 2
            assert diagnostics["cache_storage_bytes"] > 0
            assert cached_thread is None
            assert stale_state is None
            assert await cache.get_thread_id_for_event(_ROOM_ID, _THREAD_ID) is None

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
            assert rows == [
                (other_namespace, json.dumps(_message_event(_CHILD_ID, thread_id=_THREAD_ID))),
            ]
            cursor = await db.execute(
                "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
            )
            assert await cursor.fetchone() == ("4",)
            await cursor.close()
        finally:
            await cache.close()

        other_cache = PostgresEventCache(database_url=database_url, namespace=other_namespace)
        await other_cache.initialize()
        try:
            other_diagnostics = other_cache.runtime_diagnostics()
            assert other_diagnostics["cache_normalized_legacy_thread_payload_rows"] == 2
            cursor = await other_cache._runtime.require_db().execute(
                """
                SELECT event_json
                FROM mindroom_event_cache_thread_events
                WHERE namespace = %s AND event_id = %s
                """,
                (other_namespace, _CHILD_ID),
            )
            assert await cursor.fetchone() is None
            await cursor.close()
            assert await other_cache.get_thread_events(_ROOM_ID, _THREAD_ID) is None
            other_stale_state = await other_cache.get_thread_cache_state(_ROOM_ID, _THREAD_ID)
            assert other_stale_state is None
        finally:
            await other_cache.close()


@pytest.mark.asyncio
async def test_sender_backfill_skips_a_namespace_it_already_completed(
    postgres_event_cache_url: str,
) -> None:
    """The second start does not rescan, which is the whole point of the marker.

    Nothing indexes ``sender``, so an ungated backfill is a heap scan of the namespace on every
    initialization inside the advisory-lock transaction that serializes other principals' startup,
    and it rewrites every payload that genuinely carries no sender forever. Asserted by behaviour
    rather than timing: a row forced back to '' after the marker exists survives the next start,
    which can only happen if the statement did not run.
    """
    namespace = f"tenant_{uuid.uuid4().hex}"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        first = PostgresEventCache(database_url=database_url, namespace=namespace)
        await first.initialize()
        await first.close()

        setup = await psycopg.AsyncConnection.connect(database_url)
        try:
            await setup.execute(
                """
                INSERT INTO mindroom_event_cache_events(
                    namespace, event_id, room_id, origin_server_ts, event_json, sender, cached_at
                )
                VALUES (%s, %s, %s, %s, %s, '', 0)
                """,
                (namespace, "$after_marker", _ROOM_ID, 1000, '{"event_id":"$after_marker","sender":"@a:localhost"}'),
            )
            await setup.commit()
        finally:
            await setup.close()

        second = PostgresEventCache(database_url=database_url, namespace=namespace)
        await second.initialize()
        await second.close()

        check = await psycopg.AsyncConnection.connect(database_url)
        try:
            cursor = await check.execute(
                "SELECT sender FROM mindroom_event_cache_events WHERE namespace = %s AND event_id = %s",
                (namespace, "$after_marker"),
            )
            assert await cursor.fetchone() == ("",), "the namespace was rescanned despite its completion marker"
            await cursor.close()

            cursor = await check.execute(
                """
                SELECT value FROM mindroom_event_cache_namespace_metadata
                WHERE namespace = %s AND key = 'sender_backfilled'
                """,
                (namespace,),
            )
            assert await cursor.fetchone() == ("done",), "the marker is not namespace-scoped"
            await cursor.close()
        finally:
            await check.close()


@pytest.mark.asyncio
async def test_postgres_current_version_maintenance_avoids_exclusive_schema_lock(
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
                "LOCK TABLE mindroom_event_cache_events IN ACCESS SHARE MODE",
            )
            await maintainer.execute("SET statement_timeout = '500ms'")
            migration_result = await migrate_postgres_schema(
                maintainer,
                namespace=namespace,
                current_schema_version=_POSTGRES_EVENT_CACHE_SCHEMA_VERSION,
                target_schema_version=_POSTGRES_EVENT_CACHE_SCHEMA_VERSION,
            )
            assert migration_result.migrated_from_schema_version is None
            assert migration_result.normalized_legacy_thread_payload_rows == 0
            await maintainer.rollback()
        finally:
            await blocker.rollback()
            await blocker.close()
            await maintainer.close()


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
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_postgres_reconnect_does_not_repeat_startup_maintenance(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a closed connection avoids namespace-wide startup scans."""
    maintenance_calls = 0
    real_maintenance = postgres_event_cache.run_startup_maintenance

    async def count_maintenance(*args: object, **kwargs: object) -> object:
        nonlocal maintenance_calls
        maintenance_calls += 1
        return await real_maintenance(*args, **kwargs)

    monkeypatch.setattr(postgres_event_cache, "run_startup_maintenance", count_maintenance)
    cache = PostgresEventCache(
        database_url=postgres_event_cache_url,
        namespace=f"reconnect_{uuid.uuid4().hex}",
    )
    await cache.initialize()
    try:
        assert maintenance_calls == 1
        db = cache._runtime.db
        assert db is not None
        await db.close()

        assert await cache.get_event(_ROOM_ID, _MISSING_ID) is None
        assert maintenance_calls == 1
        assert cache.runtime_diagnostics()["cache_postgres_reconnect_count"] == 1
    finally:
        await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_state", ["missing", "changed"])
async def test_postgres_reconnect_rejects_changed_certification_generation(
    postgres_event_cache_url: str,
    metadata_state: str,
) -> None:
    """Reconnect must not certify an old sync position against replaced cache state."""
    namespace = f"reconnect_generation_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    await cache.initialize()
    expected_generation = cache.cache_generation
    assert expected_generation is not None
    admin = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        if metadata_state == "missing":
            await admin.execute(
                """
                DELETE FROM mindroom_event_cache_namespace_metadata
                WHERE namespace = %s AND key = 'certification_generation'
                """,
                (namespace,),
            )
        else:
            await admin.execute(
                """
                UPDATE mindroom_event_cache_namespace_metadata
                SET value = %s
                WHERE namespace = %s AND key = 'certification_generation'
                """,
                (uuid.uuid4().hex, namespace),
            )
        await admin.commit()

        db = cache._runtime.db
        assert db is not None
        await db.close()

        assert await cache.get_event(_ROOM_ID, _MISSING_ID) is None
        assert cache.cache_generation is None
        assert cache.durable_writes_available is False
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_postgres_disabled_reason"] == "certification_generation_changed"

        cursor = await admin.execute(
            """
            SELECT value
            FROM mindroom_event_cache_namespace_metadata
            WHERE namespace = %s AND key = 'certification_generation'
            """,
            (namespace,),
        )
        row = await cursor.fetchone()
        assert (row is None) is (metadata_state == "missing")
        if row is not None:
            assert row[0] != expected_generation
    finally:
        await admin.close()
        await cache.close()


@pytest.mark.asyncio
async def test_sender_backfills_in_every_namespace_not_just_the_first(
    postgres_event_cache_url: str,
) -> None:
    """Each principal's legacy rows get a sender, not only the one that wins the version bump.

    ``schema_version`` is global to the database while every Matrix principal owns its own
    namespace. Gating the backfill on that shared version let the first principal to start
    backfill itself, write the new version, and strand every other principal's rows at the ''
    default - which makes every event look like the same author, so a collapsed read can no longer
    tell an author's own edit from a foreign one and lets the foreign replacement win.
    """
    first = f"tenant_{uuid.uuid4().hex}"
    second = f"tenant_{uuid.uuid4().hex}"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        setup = await psycopg.AsyncConnection.connect(database_url)
        try:
            await postgres_event_cache._create_postgres_event_cache_schema(setup)
            for namespace in (first, second):
                await setup.execute(
                    """
                    INSERT INTO mindroom_event_cache_events(
                        namespace, event_id, room_id, origin_server_ts, event_json, sender, cached_at
                    )
                    VALUES (%s, %s, %s, %s, %s, '', 0)
                    """,
                    (namespace, "$legacy", _ROOM_ID, 1000, '{"event_id":"$legacy","sender":"@a:localhost"}'),
                )
            await setup.commit()
        finally:
            await setup.close()

        for namespace in (first, second):
            cache = PostgresEventCache(database_url=database_url, namespace=namespace)
            await cache.initialize()
            await cache.close()

        check = await psycopg.AsyncConnection.connect(database_url)
        try:
            for namespace in (first, second):
                cursor = await check.execute(
                    "SELECT sender FROM mindroom_event_cache_events WHERE namespace = %s",
                    (namespace,),
                )
                assert await cursor.fetchone() == ("@a:localhost",), f"{namespace} kept the sender default"
                await cursor.close()
        finally:
            await check.close()


@pytest.mark.asyncio
async def test_sender_backfill_survives_a_payload_jsonb_cannot_parse(
    postgres_event_cache_url: str,
) -> None:
    r"""Payloads PostgreSQL cannot cast must not stop a namespace from ever initializing.

    ``event_json`` is always valid JSON because ``json.dumps`` wrote it, but ``jsonb`` accepts less
    than JSON does. A NUL escape raises ``UntranslatableCharacter`` and a lone surrogate raises
    ``InvalidTextRepresentation``, either of which aborts the whole statement - and with it the
    migration transaction that ``_initialize_postgres_event_cache_db`` re-raises, so one such row
    locks that namespace out of its cache permanently, on every restart. The NUL case is not
    theoretical: it turns up when tool output captures binary content into a message body.

    Both senders must still be recovered: decoding in Python is the exact inverse of the dump that
    wrote the row, so an unparseable-to-PostgreSQL payload is not an unreadable one.
    """
    namespace = f"tenant_{uuid.uuid4().hex}"
    # A real payload shape: a message body carrying a NUL escape, next to an ordinary event.
    poisoned = json.dumps(
        {"event_id": "$binary", "sender": "@tool:localhost", "content": {"body": "RIFF" + chr(0) + "WAVE"}},
    )
    # A lone surrogate is the other shape PostgreSQL rejects and json.dumps still emits.
    surrogate = json.dumps(
        {"event_id": "$surrogate", "sender": "@lone:localhost", "content": {"body": chr(0xD800)}},
    )
    assert "\\u0000" in poisoned, "the fixture must carry the escape the cast rejects"
    assert "\\ud800" in surrogate, "the fixture must carry a lone surrogate"
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        setup = await psycopg.AsyncConnection.connect(database_url)
        try:
            await postgres_event_cache._create_postgres_event_cache_schema(setup)
            for event_id, event_json in (
                ("$binary", poisoned),
                ("$surrogate", surrogate),
                ("$ordinary", '{"event_id":"$ordinary","sender":"@a:localhost"}'),
            ):
                await setup.execute(
                    """
                    INSERT INTO mindroom_event_cache_events(
                        namespace, event_id, room_id, origin_server_ts, event_json, sender, cached_at
                    )
                    VALUES (%s, %s, %s, %s, %s, '', 0)
                    """,
                    (namespace, event_id, _ROOM_ID, 1000, event_json),
                )
            await setup.commit()
        finally:
            await setup.close()

        cache = PostgresEventCache(database_url=database_url, namespace=namespace)
        await cache.initialize()
        await cache.close()

        check = await psycopg.AsyncConnection.connect(database_url)
        try:
            cursor = await check.execute(
                "SELECT event_id, sender FROM mindroom_event_cache_events WHERE namespace = %s ORDER BY event_id",
                (namespace,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        finally:
            await check.close()
        # Decoding in Python recovers the sender from every payload PostgreSQL cannot cast.
        assert rows == [
            ("$binary", "@tool:localhost"),
            ("$ordinary", "@a:localhost"),
            ("$surrogate", "@lone:localhost"),
        ]


@pytest.mark.asyncio
async def test_sender_backfill_batches_each_page_into_one_statement(
    postgres_event_cache_url: str,
) -> None:
    """The backfill must cost one UPDATE per page, not one per row.

    It runs inside the advisory-lock transaction that serializes every other principal's startup,
    so a round trip per row turns a large namespace into a multi-minute stall for every principal
    behind it. Pagination alone does not fix that: it bounds how many payloads are held in memory
    at once, not how many statements are issued.
    """
    namespace = f"tenant_{uuid.uuid4().hex}"
    # Enough rows to need more than one page, so pagination and batching are both exercised.
    row_count = _SENDER_BACKFILL_PAGE_ROWS + 7
    async with _isolated_postgres_database(postgres_event_cache_url) as database_url:
        setup = await psycopg.AsyncConnection.connect(database_url)
        try:
            await postgres_event_cache._create_postgres_event_cache_schema(setup)
            for index in range(row_count):
                await setup.execute(
                    """
                    INSERT INTO mindroom_event_cache_events(
                        namespace, event_id, room_id, origin_server_ts, event_json, sender, cached_at
                    )
                    VALUES (%s, %s, %s, %s, %s, '', 0)
                    """,
                    (
                        namespace,
                        f"$event{index:05d}",
                        _ROOM_ID,
                        index,
                        json.dumps({"event_id": f"$event{index:05d}", "sender": f"@user{index}:localhost"}),
                    ),
                )
            await setup.commit()
        finally:
            await setup.close()

        db = await psycopg.AsyncConnection.connect(database_url)
        statements: list[str] = []
        original_execute = db.execute

        async def counting_execute(query, params=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
            statements.append(str(query))
            return await original_execute(query, params, **kwargs)

        try:
            db.execute = counting_execute  # type: ignore[method-assign]
            await _backfill_collapsed_read_columns(db, namespace=namespace)
            db.execute = original_execute  # type: ignore[method-assign]
            await db.commit()

            cursor = await db.execute(
                "SELECT count(*) FROM mindroom_event_cache_events WHERE namespace = %s AND sender <> ''",
                (namespace,),
            )
            backfilled = (await cursor.fetchone())[0]
            await cursor.close()
        finally:
            await db.close()

        assert backfilled == row_count, "pagination must reach every row, not just the first page"
        updates = [statement for statement in statements if statement.lstrip().startswith("UPDATE")]
        pages = -(-row_count // _SENDER_BACKFILL_PAGE_ROWS)
        assert len(updates) == pages, f"expected one UPDATE per page ({pages}), issued {len(updates)}"
