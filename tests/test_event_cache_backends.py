"""Runtime selection for Matrix event-cache backends."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

import psycopg
import pytest

from mindroom.config.matrix import CacheConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.logging_config import get_logger
from mindroom.matrix.cache import postgres_event_cache_threads, sqlite_event_cache, sqlite_event_cache_threads
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.postgres_event_cache import (
    PostgresEventCache,
    _create_postgres_event_cache_schema,
    _FlushedPendingWrites,
    _initialize_postgres_event_cache_db,
    _is_transient_postgres_failure,
    _PostgresEventCacheRuntime,
)
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_cache_state import (
    THREAD_HISTORY_TRUST_METADATA_KEY,
    THREAD_HISTORY_TRUST_VERSION,
)
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.runtime_support import (
    OwnedRuntimeSupport,
    StartupThreadPrewarmRegistry,
    _build_event_cache,
    _event_cache_runtime_identity,
    _EventCacheRuntimeIdentity,
    _initialize_event_cache_best_effort,
    sync_owned_runtime_support,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import aiosqlite

    from mindroom.matrix.cache import ConversationEventCache


def _message_event(
    *,
    event_id: str,
    sender: str,
    body: str,
    origin_server_ts: int,
    thread_id: str | None = None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
    }
    if thread_id is not None:
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
        }
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "content": content,
    }


def _edit_event(
    *,
    event_id: str,
    sender: str,
    original_event_id: str,
    body: str,
    origin_server_ts: int,
) -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "content": {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {
                "msgtype": "m.text",
                "body": body,
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
        },
    }


async def _seed_payload_identity_rows(
    cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Seed one thread child and one newer recent-room row for raw corruption tests."""
    root = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="root",
        origin_server_ts=1000,
    )
    reply = _message_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="reply",
        origin_server_ts=2000,
        thread_id=thread_id,
    )
    valid_recent = _message_event(
        event_id="$valid-recent",
        sender="@agent:localhost",
        body="valid",
        origin_server_ts=3000,
    )
    poisoned_recent = _message_event(
        event_id="$poisoned-recent",
        sender="@agent:localhost",
        body="poisoned",
        origin_server_ts=4000,
    )
    await _replace_thread(cache, room_id, thread_id, [root, reply])
    await cache.store_events_batch(
        [
            ("$valid-recent", room_id, valid_recent),
            ("$poisoned-recent", room_id, poisoned_recent),
        ],
    )
    return reply, poisoned_recent


async def _assert_payload_identity_poison_is_rejected(
    cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
) -> None:
    """Assert thread reads fail closed while recent reads skip poison before limiting."""
    with pytest.raises(ValueError, match="does not match its authoritative index"):
        await cache.get_thread_events(room_id, thread_id)

    recent = await cache.get_recent_room_events(
        room_id,
        event_type="m.room.message",
        since_ts_ms=0,
        limit=1,
    )

    assert [event["event_id"] for event in recent] == ["$valid-recent"]


def _postgres_schema_url(database_url: str, schema_name: str) -> str:
    """Return a disposable URL pinned to one isolated PostgreSQL schema."""
    separator = "&" if "?" in database_url else "?"
    options = quote(f"-csearch_path={schema_name}", safe="")
    return f"{database_url}{separator}options={options}"


def _seed_sqlite_v11_schema(db_path: Path) -> None:
    """Create the complete immediate-predecessor SQLite schema."""
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE cache_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE thread_events (
            room_id TEXT NOT NULL, thread_id TEXT NOT NULL, event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL, write_seq INTEGER NOT NULL,
            PRIMARY KEY (room_id, event_id)
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, origin_server_ts INTEGER NOT NULL,
            event_json TEXT NOT NULL, cached_at REAL NOT NULL, write_seq INTEGER NOT NULL
        );
        CREATE TABLE event_edits (
            edit_event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL, origin_server_ts INTEGER NOT NULL
        );
        CREATE TABLE event_threads (
            room_id TEXT NOT NULL, event_id TEXT NOT NULL, thread_id TEXT NOT NULL,
            PRIMARY KEY (room_id, event_id)
        );
        CREATE TABLE redacted_events (
            room_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY (room_id, event_id)
        );
        CREATE TABLE mxc_text_cache (
            mxc_url TEXT PRIMARY KEY, text_content TEXT NOT NULL, cached_at REAL NOT NULL
        );
        CREATE TABLE thread_cache_state (
            room_id TEXT NOT NULL, thread_id TEXT NOT NULL, validated_at REAL,
            invalidated_at REAL, invalidation_reason TEXT, PRIMARY KEY (room_id, thread_id)
        );
        CREATE TABLE room_cache_state (
            room_id TEXT PRIMARY KEY, invalidated_at REAL, invalidation_reason TEXT
        );
        INSERT INTO cache_metadata VALUES ('write_sequence', '1');
        INSERT INTO cache_metadata VALUES ('certification_generation', 'sqlite-v11-generation');
        INSERT INTO events VALUES (
            '$legacy', '!legacy:localhost', 1, '{}', 1, 1
        );
        PRAGMA user_version = 11;
        """,
    )
    db.commit()
    db.close()


async def _seed_postgres_v1_schema(database_url: str, schema_name: str) -> str:
    """Create a minimal legacy schema in the disposable PostgreSQL test database."""
    admin = await psycopg.AsyncConnection.connect(database_url)
    await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    await admin.commit()
    await admin.close()
    isolated_url = _postgres_schema_url(database_url, schema_name)
    db = await psycopg.AsyncConnection.connect(isolated_url)
    await db.execute("CREATE SEQUENCE mindroom_event_cache_write_seq")
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_events (
            namespace TEXT NOT NULL,
            event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            origin_server_ts BIGINT NOT NULL,
            event_json TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            write_seq BIGINT NOT NULL DEFAULT nextval('mindroom_event_cache_write_seq'),
            PRIMARY KEY (namespace, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_event_edits (
            namespace TEXT NOT NULL,
            edit_event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            origin_server_ts BIGINT NOT NULL,
            PRIMARY KEY (namespace, edit_event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_mxc_text (
            namespace TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            text_content TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (namespace, mxc_url)
        )
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
        CREATE TABLE mindroom_event_cache_room_state (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            invalidated_at DOUBLE PRECISION,
            invalidation_reason TEXT,
            PRIMARY KEY (namespace, room_id)
        )
        """,
    )
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_metadata(key, value)
        VALUES ('schema_version', '1')
        """,
    )
    for namespace in ("legacy_a", "legacy_b"):
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_events(
                namespace, event_id, room_id, origin_server_ts, event_json, cached_at
            )
            VALUES (%s, '$legacy', '!legacy:localhost', 1, '{}', 1)
            """,
            (namespace,),
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_mxc_text(namespace, mxc_url, text_content, cached_at)
            VALUES (%s, 'mxc://legacy/value', 'legacy plaintext', 1)
            """,
            (namespace,),
        )
    await db.commit()
    await db.close()
    return isolated_url


async def _seed_postgres_v2_schema(database_url: str, schema_name: str) -> str:
    """Create the complete immediate-predecessor PostgreSQL schema."""
    admin = await psycopg.AsyncConnection.connect(database_url)
    await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    await admin.commit()
    await admin.close()
    isolated_url = _postgres_schema_url(database_url, schema_name)
    db = await psycopg.AsyncConnection.connect(isolated_url)
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )
    await _create_postgres_event_cache_schema(db)
    await db.execute("DROP TABLE mindroom_event_cache_event_mxc_references")
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_events
            DROP CONSTRAINT mindroom_event_cache_events_pkey,
            ADD PRIMARY KEY (namespace, event_id)
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_event_edits
            DROP CONSTRAINT mindroom_event_cache_event_edits_pkey,
            ADD PRIMARY KEY (namespace, edit_event_id)
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_room_state
            DROP COLUMN membership_state,
            DROP COLUMN membership_epoch
        """,
    )
    await db.execute("DROP TABLE mindroom_event_cache_mxc_text")
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_mxc_text (
            namespace TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            text_content TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (namespace, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_metadata(key, value)
        VALUES ('schema_version', '2')
        """,
    )
    for namespace in ("legacy_a", "legacy_b"):
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_events(
                namespace, event_id, room_id, origin_server_ts, event_json, cached_at
            )
            VALUES (%s, '$legacy', '!legacy:localhost', 1, '{}', 1)
            """,
            (namespace,),
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_mxc_text(namespace, mxc_url, text_content, cached_at)
            VALUES (%s, 'mxc://legacy/value', 'legacy plaintext', 1)
            """,
            (namespace,),
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_namespace_metadata(namespace, key, value)
            VALUES (%s, 'certification_generation', %s)
            """,
            (namespace, f"{namespace}-generation"),
        )
    await db.commit()
    await db.close()
    return isolated_url


async def _seed_postgres_v3_schema(database_url: str, schema_name: str) -> str:
    """Create schema v3 with its existing narrowing latest-edit index."""
    admin = await psycopg.AsyncConnection.connect(database_url)
    await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    await admin.commit()
    await admin.close()
    isolated_url = _postgres_schema_url(database_url, schema_name)
    db = await psycopg.AsyncConnection.connect(isolated_url)
    await db.execute(
        """
        CREATE TABLE mindroom_event_cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )
    await _create_postgres_event_cache_schema(db)
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_metadata(key, value)
        VALUES ('schema_version', '3')
        """,
    )
    await db.commit()
    await db.close()
    return isolated_url


def _runtime_paths(tmp_path: Path, *, env: dict[str, str] | None = None) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    process_env = {
        "MATRIX_HOMESERVER": "http://localhost:8008",
        "MINDROOM_NAMESPACE": "",
    }
    if env is not None:
        process_env.update(env)
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env,
    )


async def _assert_thread_lookup_behavior(
    cache: PostgresEventCache,
    shared_cache: PostgresEventCache,
    isolated_cache: PostgresEventCache,
    *,
    room_id: str,
    thread_id: str,
    root_event: dict[str, object],
    reply_event: dict[str, object],
) -> None:
    await _replace_thread(cache, room_id, thread_id, [reply_event, root_event], validated_at=100.0)

    cached_thread = await cache.get_thread_events(room_id, thread_id)
    assert cached_thread is not None
    assert [event["event_id"] for event in cached_thread] == [thread_id, "$reply"]
    assert await cache.get_recent_room_thread_ids(room_id, limit=5) == [thread_id]
    assert await cache.get_event(room_id, "$reply") == reply_event
    assert await cache.get_thread_id_for_event(room_id, "$reply") == thread_id
    assert await cache.get_thread_id_for_event(room_id, thread_id) == thread_id

    assert await shared_cache.get_thread_events(room_id, thread_id) == cached_thread
    assert await shared_cache.get_event(room_id, "$reply") == reply_event
    assert await shared_cache.get_thread_id_for_event(room_id, "$reply") == thread_id

    assert await isolated_cache.get_thread_events(room_id, thread_id) is None
    assert await isolated_cache.get_event(room_id, "$reply") is None


async def _assert_edit_snapshot_and_mxc_behavior(
    cache: PostgresEventCache,
    *,
    room_id: str,
    thread_id: str,
    sender: str,
    old_edit: dict[str, object],
    latest_edit: dict[str, object],
) -> None:
    await cache.store_events_batch(
        [
            ("$edit-old", room_id, old_edit),
            ("$edit-latest", room_id, latest_edit),
        ],
    )
    assert (
        await cache.get_latest_edit(
            room_id,
            "$reply",
            sender=sender,
            event_type="m.room.message",
        )
        == latest_edit
    )
    snapshot = await cache.get_latest_agent_message_snapshot(
        room_id,
        thread_id,
        sender,
        runtime_started_at=None,
    )
    assert snapshot is not None
    assert snapshot.content["body"] == "latest edit"
    assert snapshot.origin_server_ts == 1010

    mxc_owner = _message_event(
        event_id="$mxc-owner",
        sender=sender,
        body="preview",
        origin_server_ts=1040,
    )
    mxc_owner["content"] = {
        "body": "preview",
        "msgtype": "m.file",
        "url": "mxc://localhost/media",
        "io.mindroom.long_text": {
            "version": 2,
            "encoding": "matrix_event_content_json",
        },
    }
    await cache.store_event("$mxc-owner", room_id, mxc_owner)
    assert await cache.store_mxc_text(
        room_id,
        "$mxc-owner",
        "mxc://localhost/media",
        "downloaded text",
    )
    assert await cache.get_mxc_text(room_id, "$mxc-owner", "mxc://localhost/media") == "downloaded text"


async def _assert_staleness_and_redaction_behavior(
    cache: PostgresEventCache,
    *,
    room_id: str,
    thread_id: str,
    sender: str,
    latest_edit: dict[str, object],
) -> None:
    await cache.mark_thread_stale(room_id, thread_id, reason="live_thread_mutation")
    stale_state = await cache.get_thread_cache_state(room_id, thread_id)
    assert stale_state is not None
    assert stale_state.invalidated_at is not None
    assert stale_state.invalidation_reason == "live_thread_mutation"
    assert await cache.revalidate_thread_after_incremental_update(room_id, thread_id) is True
    fresh_state = await cache.get_thread_cache_state(room_id, thread_id)
    assert fresh_state is not None
    assert fresh_state.invalidated_at is None
    assert fresh_state.invalidation_reason is None

    assert await cache.redact_event(room_id, "$reply") is True
    assert await cache.get_event(room_id, "$reply") is None
    assert (
        await cache.get_latest_edit(
            room_id,
            "$reply",
            sender=sender,
            event_type="m.room.message",
        )
        is None
    )
    redacted_thread = await cache.get_thread_events(room_id, thread_id)
    assert redacted_thread is not None
    assert [event["event_id"] for event in redacted_thread] == [thread_id]

    await cache.store_event("$edit-latest", room_id, latest_edit)
    assert (
        await cache.get_latest_edit(
            room_id,
            "$reply",
            sender=sender,
            event_type="m.room.message",
        )
        is None
    )


def test_cache_config_resolves_postgres_url_and_namespace_from_runtime_env(tmp_path: Path) -> None:
    """Postgres cache config should keep secrets in runtime env and scope rows by namespace."""
    runtime_paths = _runtime_paths(
        tmp_path,
        env={
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:test@localhost/mindroom",
            "MINDROOM_NAMESPACE": "tenant-a",
        },
    )
    cache_config = CacheConfig(backend="postgres")

    assert cache_config.resolve_postgres_database_url(runtime_paths) == "postgresql://cache:test@localhost/mindroom"
    assert cache_config.resolve_namespace(runtime_paths) == "tenant-a"


def test_cache_config_accepts_custom_secret_filtered_postgres_url_env(tmp_path: Path) -> None:
    """Custom Postgres DSN env names must use the secret-filtered DATABASE_URL shape."""
    runtime_paths = _runtime_paths(
        tmp_path,
        env={
            "MINDROOM_CACHE_DATABASE_URL": "postgresql://cache:test@localhost/mindroom",
        },
    )
    cache_config = CacheConfig(backend="postgres", database_url_env="MINDROOM_CACHE_DATABASE_URL")

    assert cache_config.resolve_postgres_database_url(runtime_paths) == "postgresql://cache:test@localhost/mindroom"


def test_cache_config_rejects_custom_postgres_url_env_without_database_url_suffix() -> None:
    """Unsafe custom DSN env names would bypass runtime secret filters."""
    with pytest.raises(ValueError, match="DATABASE_URL"):
        CacheConfig(backend="postgres", database_url_env="MINDROOM_CACHE_URL")


def test_build_event_cache_defaults_to_sqlite(tmp_path: Path) -> None:
    """SQLite should remain the default cache backend for local installs."""
    runtime_paths = _runtime_paths(tmp_path)
    cache = _build_event_cache(CacheConfig(), runtime_paths)

    assert isinstance(cache, SqliteEventCache)
    assert cache.db_path == tmp_path / "mindroom_data" / "event_cache.db"


@pytest.mark.asyncio
async def test_sqlite_event_cache_write_operation_rolls_back_cancelled_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation inside a SQLite cache write must not leave the shared connection in a transaction."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    cancel_reason = "stop requested"
    db = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(side_effect=RuntimeError("rollback failed")),
    )

    @asynccontextmanager
    async def acquire_db_operation() -> AsyncIterator[object]:
        yield db

    monkeypatch.setattr(
        cache,
        "_runtime",
        SimpleNamespace(
            is_disabled=False,
            acquire_db_operation=acquire_db_operation,
            is_principal_disabled=Mock(return_value=False),
            is_room_departed=Mock(return_value=False),
            has_pending_principal_purge=Mock(return_value=False),
            has_pending_room_purge=Mock(return_value=False),
        ),
    )

    async def cancelled_writer(_db: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(
        sqlite_event_cache_threads,
        "load_room_membership_locked",
        AsyncMock(return_value=("joined", 0)),
    )
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await cache._write_operation(
            "!room:example.test",
            operation="cancelled_writer",
            disabled_result=None,
            writer=cancelled_writer,
        )

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sqlite_event_cache_initialize_closes_db_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during SQLite cache initialization must close the half-initialized connection."""
    cancel_reason = "init cancelled"
    db = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("close failed")),
        execute=AsyncMock(),
    )

    async def connect(_db_path: Path) -> object:
        return db

    async def prepare_event_cache_schema(_db: object, *, db_path: Path) -> tuple[int | None, bool, int]:
        _ = db_path
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(sqlite_event_cache.aiosqlite, "connect", connect)
    monkeypatch.setattr(sqlite_event_cache, "_prepare_event_cache_schema", prepare_event_cache_schema)

    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await sqlite_event_cache._initialize_event_cache_db(tmp_path / "event_cache.db")

    db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sqlite_v10_reset_is_atomic_and_creates_new_generation(tmp_path: Path) -> None:
    """SQLite v10 contents reset transactionally into the current principal-owned schema."""
    db_path = tmp_path / "event_cache.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY, event_json TEXT)")
    legacy.execute("INSERT INTO events VALUES ('$legacy', '{}')")
    legacy.execute("PRAGMA user_version = 10")
    legacy.commit()
    legacy.close()

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        assert cache.cache_generation
        assert await cache.get_event("!room:localhost", "$legacy") is None
        assert cache._runtime.db is not None
        version_row = await (await cache._runtime.db.execute("PRAGMA user_version")).fetchone()
        assert version_row == (12,)
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_sqlite_v10_reset_rolls_back_on_schema_creation_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot commit a cache reset while leaving an old checkpoint plausible."""
    db_path = tmp_path / "event_cache.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY, event_json TEXT)")
    legacy.execute("INSERT INTO events VALUES ('$legacy', '{}')")
    legacy.execute("PRAGMA user_version = 10")
    legacy.commit()
    legacy.close()

    cancel_reason = "schema cancelled"
    create_schema = sqlite_event_cache._create_event_cache_schema

    async def cancelled_schema(db: aiosqlite.Connection) -> None:
        await create_schema(db)
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(sqlite_event_cache, "_create_event_cache_schema", cancelled_schema)
    with pytest.raises(asyncio.CancelledError, match="schema cancelled"):
        await sqlite_event_cache._initialize_event_cache_db(db_path)

    inspected = sqlite3.connect(db_path)
    try:
        assert inspected.execute("PRAGMA user_version").fetchone() == (10,)
        assert inspected.execute("SELECT event_id FROM events").fetchall() == [("$legacy",)]
    finally:
        inspected.close()


@pytest.mark.asyncio
async def test_sqlite_v11_reset_rotates_generation(tmp_path: Path) -> None:
    """The immediate-predecessor cache resets with a new checkpoint generation."""
    db_path = tmp_path / "event_cache.db"
    _seed_sqlite_v11_schema(db_path)

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        assert await cache.get_event("!legacy:localhost", "$legacy") is None
        assert cache.runtime_diagnostics()["cache_schema_destructive_reset"] is True
        assert cache._runtime.db is not None
        version_row = await (await cache._runtime.db.execute("PRAGMA user_version")).fetchone()
        generation_row = await (
            await cache._runtime.db.execute(
                "SELECT value FROM cache_metadata WHERE key = 'certification_generation'",
            )
        ).fetchone()
        assert version_row == (12,)
        assert generation_row is not None
        assert generation_row[0] != "sqlite-v11-generation"
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_sqlite_v11_reset_rolls_back_generation_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled v11 reset preserves its rows, version, and checkpoint generation."""
    db_path = tmp_path / "event_cache.db"
    _seed_sqlite_v11_schema(db_path)
    cancel_reason = "migration cancelled"

    async def cancel_maintenance(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(sqlite_event_cache, "run_startup_maintenance", cancel_maintenance)
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await sqlite_event_cache._initialize_event_cache_db(db_path)

    inspected = sqlite3.connect(db_path)
    try:
        assert inspected.execute("PRAGMA user_version").fetchone() == (11,)
        assert inspected.execute("SELECT event_id FROM events").fetchall() == [("$legacy",)]
        assert inspected.execute(
            "SELECT value FROM cache_metadata WHERE key = 'certification_generation'",
        ).fetchone() == ("sqlite-v11-generation",)
        assert (
            inspected.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'event_mxc_references'",
            ).fetchone()
            is None
        )
    finally:
        inspected.close()


@pytest.mark.asyncio
async def test_sqlite_opaque_history_trust_upgrade_clears_only_certified_snapshots_once(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """The opaque-history trust upgrade must invalidate snapshots while preserving safe lookup data."""
    db_path = tmp_path / "event_cache.db"
    room_id = "!room:localhost"
    thread_id = "$root"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="root",
        origin_server_ts=1000,
    )
    reply_event = _message_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="reply",
        origin_server_ts=2000,
        thread_id=thread_id,
    )
    standalone_event = _message_event(
        event_id="$standalone",
        sender="@user:localhost",
        body="standalone",
        origin_server_ts=3000,
    )
    seeded = SqliteEventCache(db_path)
    await seeded.initialize()
    try:
        await _replace_thread(seeded, room_id, thread_id, [reply_event, root_event], validated_at=100.0)
        await seeded.store_event("$standalone", room_id, standalone_event)
        original_generation = seeded.cache_generation
        original_membership_epoch = await seeded.room_membership_epoch(room_id)
    finally:
        await seeded.close()

    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute(
            "DELETE FROM cache_metadata WHERE key = ?",
            (THREAD_HISTORY_TRUST_METADATA_KEY,),
        )
        legacy.commit()
    finally:
        legacy.close()

    upgraded = SqliteEventCache(db_path)
    await upgraded.initialize()
    try:
        upgraded_generation = upgraded.cache_generation
        assert upgraded_generation is not None
        assert upgraded_generation != original_generation
        assert await upgraded.get_thread_events(room_id, thread_id) is None
        assert await upgraded.get_thread_cache_state(room_id, thread_id) is None
        assert await upgraded.get_event(room_id, thread_id) == root_event
        assert await upgraded.get_event(room_id, "$reply") == reply_event
        assert await upgraded.get_event(room_id, "$standalone") == standalone_event
        assert await upgraded.get_thread_id_for_event(room_id, thread_id) == thread_id
        assert await upgraded.get_thread_id_for_event(room_id, "$reply") == thread_id
        assert await upgraded.room_membership_epoch(room_id) == original_membership_epoch
    finally:
        await upgraded.close()

    reopened = SqliteEventCache(db_path)
    await reopened.initialize()
    try:
        assert reopened.cache_generation == upgraded_generation
        assert await reopened.get_event(room_id, "$standalone") == standalone_event
        assert await reopened.get_thread_events(room_id, thread_id) is None
        assert reopened._runtime.db is not None
        trust_row = await (
            await reopened._runtime.db.execute(
                "SELECT value FROM cache_metadata WHERE key = ?",
                (THREAD_HISTORY_TRUST_METADATA_KEY,),
            )
        ).fetchone()
        assert trust_row == (THREAD_HISTORY_TRUST_VERSION,)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_opaque_history_trust_upgrade_rolls_back_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must roll back the trust marker, snapshot deletion, and generation rotation."""
    db_path = tmp_path / "event_cache.db"
    room_id = "!room:localhost"
    thread_id = "$root"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="root",
        origin_server_ts=1000,
    )
    seeded = SqliteEventCache(db_path)
    await seeded.initialize()
    try:
        await _replace_thread(seeded, room_id, thread_id, [root_event], validated_at=100.0)
    finally:
        await seeded.close()

    legacy = sqlite3.connect(db_path)
    try:
        original_generation = legacy.execute(
            "SELECT value FROM cache_metadata WHERE key = 'certification_generation'",
        ).fetchone()
        assert original_generation is not None
        legacy.execute(
            "DELETE FROM cache_metadata WHERE key = ?",
            (THREAD_HISTORY_TRUST_METADATA_KEY,),
        )
        legacy.commit()
    finally:
        legacy.close()

    cancel_reason = "maintenance cancelled"

    async def cancel_maintenance(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(sqlite_event_cache, "run_startup_maintenance", cancel_maintenance)
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await sqlite_event_cache._initialize_event_cache_db(db_path)

    inspected = sqlite3.connect(db_path)
    try:
        assert (
            inspected.execute(
                "SELECT value FROM cache_metadata WHERE key = ?",
                (THREAD_HISTORY_TRUST_METADATA_KEY,),
            ).fetchone()
            is None
        )
        assert (
            inspected.execute(
                "SELECT value FROM cache_metadata WHERE key = 'certification_generation'",
            ).fetchone()
            == original_generation
        )
        assert inspected.execute(
            "SELECT event_id FROM thread_events WHERE room_id = ? AND thread_id = ?",
            (room_id, thread_id),
        ).fetchall() == [(thread_id,)]
        assert inspected.execute(
            "SELECT validated_at FROM thread_cache_state WHERE room_id = ? AND thread_id = ?",
            (room_id, thread_id),
        ).fetchone() == (100.0,)
    finally:
        inspected.close()


def test_build_event_cache_uses_postgres_when_configured(tmp_path: Path) -> None:
    """The runtime factory should construct the Postgres cache backend only when requested."""
    runtime_paths = _runtime_paths(
        tmp_path,
        env={
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:test@localhost/mindroom",
            "MINDROOM_NAMESPACE": "tenant-a",
        },
    )
    cache = _build_event_cache(CacheConfig(backend="postgres"), runtime_paths)

    assert isinstance(cache, PostgresEventCache)
    assert cache.database_url == "postgresql://cache:test@localhost/mindroom"
    assert cache.namespace == "tenant-a"


@pytest.mark.asyncio
async def test_postgres_event_cache_initialize_attempts_cleanup_without_masking_cancelled_schema_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during Postgres startup schema creation must not leak an open transaction."""
    cancel_reason = "startup cancelled"
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(side_effect=RuntimeError("rollback failed")),
        close=AsyncMock(side_effect=RuntimeError("close failed")),
        execute=AsyncMock(),
    )

    async def connect(_database_url: str) -> object:
        return db

    async def create_schema(_db: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr("mindroom.matrix.cache.postgres_event_cache.psycopg.AsyncConnection.connect", connect)
    monkeypatch.setattr(
        "mindroom.matrix.cache.postgres_event_cache._postgres_schema_version",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("mindroom.matrix.cache.postgres_event_cache._create_postgres_event_cache_schema", create_schema)

    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await _initialize_postgres_event_cache_db(
            "postgresql://cache:test@localhost/mindroom",
            namespace="tenant-a",
        )

    db.rollback.assert_awaited_once()
    db.close.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_v1_migration_is_concurrent_and_namespace_preserving(
    postgres_event_cache_url: str,
) -> None:
    """Advisory-locked migrations preserve namespaces but drop ownerless plaintext."""
    schema_name = f"cache_migration_{uuid.uuid4().hex}"
    isolated_url = await _seed_postgres_v1_schema(postgres_event_cache_url, schema_name)
    cache_a = PostgresEventCache(database_url=isolated_url, namespace="runtime_a")
    cache_b = PostgresEventCache(database_url=isolated_url, namespace="runtime_b")
    try:
        await asyncio.gather(cache_a.initialize(), cache_b.initialize())
        db = await psycopg.AsyncConnection.connect(isolated_url)
        version = await (
            await db.execute(
                "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
            )
        ).fetchone()
        namespaces = await (
            await db.execute(
                "SELECT namespace FROM mindroom_event_cache_events ORDER BY namespace",
            )
        ).fetchall()
        legacy_plaintext = await (
            await db.execute(
                """
            SELECT namespace, room_id
            FROM mindroom_event_cache_mxc_text
            ORDER BY namespace
            """,
            )
        ).fetchall()
        membership_columns = await (
            await db.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'mindroom_event_cache_room_state'
                  AND column_name IN ('membership_state', 'membership_epoch')
                ORDER BY column_name
                """,
            )
        ).fetchall()
        await db.close()
        assert version == ("3",)
        assert namespaces == [("legacy_a",), ("legacy_b",)]
        assert legacy_plaintext == []
        assert membership_columns == [("membership_epoch",), ("membership_state",)]
    finally:
        await asyncio.gather(cache_a.close(), cache_b.close())


@pytest.mark.asyncio
async def test_postgres_v1_migration_rolls_back_on_cancellation(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled PostgreSQL DDL leaves the legacy version and constraints intact."""
    schema_name = f"cache_migration_cancel_{uuid.uuid4().hex}"
    isolated_url = await _seed_postgres_v1_schema(postgres_event_cache_url, schema_name)
    from mindroom.matrix.cache import postgres_event_cache as postgres_module  # noqa: PLC0415

    original_migration = postgres_module._migrate_postgres_event_cache_security_schema
    cancel_reason = "migration cancelled"

    async def cancelled_migration(db: object) -> None:
        await original_migration(cast("psycopg.AsyncConnection", db))
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(postgres_module, "_migrate_postgres_event_cache_security_schema", cancelled_migration)
    cache = PostgresEventCache(database_url=isolated_url, namespace="runtime")
    with pytest.raises(asyncio.CancelledError, match="migration cancelled"):
        await cache.initialize()

    db = await psycopg.AsyncConnection.connect(isolated_url)
    version = await (
        await db.execute(
            "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
        )
    ).fetchone()
    columns = await (
        await db.execute(
            """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'mindroom_event_cache_mxc_text'
        ORDER BY ordinal_position
        """,
        )
    ).fetchall()
    legacy_plaintext = await (
        await db.execute(
            """
        SELECT namespace, mxc_url, text_content
        FROM mindroom_event_cache_mxc_text
        ORDER BY namespace
        """,
        )
    ).fetchall()
    await db.close()
    assert version == ("1",)
    assert "room_id" not in {str(row[0]) for row in columns}
    assert legacy_plaintext == [
        ("legacy_a", "mxc://legacy/value", "legacy plaintext"),
        ("legacy_b", "mxc://legacy/value", "legacy plaintext"),
    ]


@pytest.mark.asyncio
async def test_postgres_v2_migration_is_namespace_preserving(
    postgres_event_cache_url: str,
) -> None:
    """The immediate-predecessor schema preserves scoped rows and generations."""
    schema_name = f"cache_migration_v2_{uuid.uuid4().hex}"
    isolated_url = await _seed_postgres_v2_schema(postgres_event_cache_url, schema_name)
    cache_a = PostgresEventCache(database_url=isolated_url, namespace="runtime_a")
    cache_b = PostgresEventCache(database_url=isolated_url, namespace="runtime_b")
    try:
        await asyncio.gather(cache_a.initialize(), cache_b.initialize())
        db = await psycopg.AsyncConnection.connect(isolated_url)
        version = await (
            await db.execute(
                "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
            )
        ).fetchone()
        namespaces = await (
            await db.execute(
                "SELECT namespace FROM mindroom_event_cache_events ORDER BY namespace",
            )
        ).fetchall()
        plaintext = await (await db.execute("SELECT namespace FROM mindroom_event_cache_mxc_text")).fetchall()
        generations = await (
            await db.execute(
                """
                SELECT namespace, value
                FROM mindroom_event_cache_namespace_metadata
                WHERE namespace LIKE 'legacy_%' AND key = 'certification_generation'
                ORDER BY namespace
                """,
            )
        ).fetchall()
        membership_columns = await (
            await db.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'mindroom_event_cache_room_state'
                  AND column_name IN ('membership_state', 'membership_epoch')
                ORDER BY column_name
                """,
            )
        ).fetchall()
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_events(
                namespace, event_id, room_id, origin_server_ts, event_json, cached_at
            )
            VALUES ('legacy_a', '$legacy', '!other:localhost', 2, '{}', 2)
            """,
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_mxc_text(namespace, room_id, mxc_url, text_content, cached_at)
            VALUES
                ('legacy_a', '!legacy:localhost', 'mxc://legacy/reused', 'a', 2),
                ('legacy_a', '!other:localhost', 'mxc://legacy/reused', 'b', 2)
            """,
        )
        event_rooms = await (
            await db.execute(
                """
                SELECT room_id
                FROM mindroom_event_cache_events
                WHERE namespace = 'legacy_a' AND event_id = '$legacy'
                ORDER BY room_id
                """,
            )
        ).fetchall()
        mxc_rooms = await (
            await db.execute(
                """
                SELECT room_id, text_content
                FROM mindroom_event_cache_mxc_text
                WHERE namespace = 'legacy_a' AND mxc_url = 'mxc://legacy/reused'
                ORDER BY room_id
                """,
            )
        ).fetchall()
        await db.rollback()
        await db.close()

        assert version == ("3",)
        assert namespaces == [("legacy_a",), ("legacy_b",)]
        assert plaintext == []
        assert generations == [
            ("legacy_a", "legacy_a-generation"),
            ("legacy_b", "legacy_b-generation"),
        ]
        assert membership_columns == [("membership_epoch",), ("membership_state",)]
        assert event_rooms == [("!legacy:localhost",), ("!other:localhost",)]
        assert mxc_rooms == [
            ("!legacy:localhost", "a"),
            ("!other:localhost", "b"),
        ]
    finally:
        await asyncio.gather(cache_a.close(), cache_b.close())


@pytest.mark.asyncio
async def test_postgres_v2_migration_rolls_back_on_cancellation(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled v2 DDL preserves predecessor data, keys, and generations."""
    schema_name = f"cache_migration_v2_cancel_{uuid.uuid4().hex}"
    isolated_url = await _seed_postgres_v2_schema(postgres_event_cache_url, schema_name)
    from mindroom.matrix.cache import postgres_event_cache as postgres_module  # noqa: PLC0415

    original_migration = postgres_module._migrate_postgres_event_cache_security_schema
    cancel_reason = "migration cancelled"

    async def cancelled_migration(db: object) -> None:
        await original_migration(cast("psycopg.AsyncConnection", db))
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr(postgres_module, "_migrate_postgres_event_cache_security_schema", cancelled_migration)
    cache = PostgresEventCache(database_url=isolated_url, namespace="runtime")
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await cache.initialize()

    db = await psycopg.AsyncConnection.connect(isolated_url)
    version = await (
        await db.execute(
            "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
        )
    ).fetchone()
    plaintext = await (
        await db.execute(
            "SELECT namespace, text_content FROM mindroom_event_cache_mxc_text ORDER BY namespace",
        )
    ).fetchall()
    generations = await (
        await db.execute(
            """
            SELECT namespace, value
            FROM mindroom_event_cache_namespace_metadata
            WHERE namespace LIKE 'legacy_%' AND key = 'certification_generation'
            ORDER BY namespace
            """,
        )
    ).fetchall()
    mxc_columns = await (
        await db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'mindroom_event_cache_mxc_text'
            ORDER BY ordinal_position
            """,
        )
    ).fetchall()
    await db.close()

    assert version == ("2",)
    assert plaintext == [
        ("legacy_a", "legacy plaintext"),
        ("legacy_b", "legacy plaintext"),
    ]
    assert generations == [
        ("legacy_a", "legacy_a-generation"),
        ("legacy_b", "legacy_b-generation"),
    ]
    assert "room_id" not in {str(row[0]) for row in mxc_columns}


@pytest.mark.asyncio
async def test_postgres_cache_generation_is_durable_and_changes_after_namespace_reset(
    postgres_event_cache_url: str,
) -> None:
    """A recreated PostgreSQL cache namespace cannot reuse an old checkpoint generation."""
    namespace = f"generation_{uuid.uuid4().hex}"
    cache = PostgresEventCache(
        database_url=postgres_event_cache_url,
        namespace=namespace,
    )
    try:
        await cache.initialize()
        first_generation = cache.cache_generation
        assert first_generation is not None

        await cache.close()
        await cache.initialize()
        assert cache.cache_generation == first_generation
    finally:
        await cache.close()

    db = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_namespace_metadata
            WHERE namespace = %s AND key = 'certification_generation'
            """,
            (namespace,),
        )
        await db.commit()
    finally:
        await db.close()

    reset_cache = PostgresEventCache(
        database_url=postgres_event_cache_url,
        namespace=namespace,
    )
    await reset_cache.initialize()
    try:
        assert reset_cache.cache_generation is not None
        assert reset_cache.cache_generation != first_generation
    finally:
        await reset_cache.close()


@pytest.mark.asyncio
async def test_postgres_opaque_history_trust_upgrade_is_namespace_scoped_and_one_time(  # noqa: PLR0915
    postgres_event_cache_url: str,
) -> None:
    """The real PostgreSQL upgrade must reset only one namespace's certified snapshots."""
    namespace = f"trust_upgrade_{uuid.uuid4().hex}"
    isolated_namespace = f"trust_upgrade_other_{uuid.uuid4().hex}"
    room_id = "!room:localhost"
    thread_id = "$root"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="root",
        origin_server_ts=1000,
    )
    reply_event = _message_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="reply",
        origin_server_ts=2000,
        thread_id=thread_id,
    )
    standalone_event = _message_event(
        event_id="$standalone",
        sender="@user:localhost",
        body="standalone",
        origin_server_ts=3000,
    )
    seeded = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    isolated = PostgresEventCache(database_url=postgres_event_cache_url, namespace=isolated_namespace)
    try:
        await seeded.initialize()
        await isolated.initialize()
        await _replace_thread(seeded, room_id, thread_id, [reply_event, root_event], validated_at=100.0)
        await seeded.store_event("$standalone", room_id, standalone_event)
        await _replace_thread(isolated, room_id, thread_id, [reply_event, root_event], validated_at=100.0)
        original_generation = seeded.cache_generation
        isolated_generation = isolated.cache_generation
        original_membership_epoch = await seeded.room_membership_epoch(room_id)
    finally:
        await seeded.close()
        await isolated.close()

    db = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_namespace_metadata
            WHERE namespace = %s AND key = %s
            """,
            (namespace, THREAD_HISTORY_TRUST_METADATA_KEY),
        )
        await db.commit()
    finally:
        await db.close()

    upgraded = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    untouched = PostgresEventCache(database_url=postgres_event_cache_url, namespace=isolated_namespace)
    try:
        await upgraded.initialize()
        await untouched.initialize()
        upgraded_generation = upgraded.cache_generation
        assert upgraded_generation is not None
        assert upgraded_generation != original_generation
        assert await upgraded.get_thread_events(room_id, thread_id) is None
        assert await upgraded.get_thread_cache_state(room_id, thread_id) is None
        assert await upgraded.get_event(room_id, thread_id) == root_event
        assert await upgraded.get_event(room_id, "$reply") == reply_event
        assert await upgraded.get_event(room_id, "$standalone") == standalone_event
        assert await upgraded.get_thread_id_for_event(room_id, thread_id) == thread_id
        assert await upgraded.get_thread_id_for_event(room_id, "$reply") == thread_id
        assert await upgraded.room_membership_epoch(room_id) == original_membership_epoch
        assert untouched.cache_generation == isolated_generation
        assert await untouched.get_thread_events(room_id, thread_id) is not None
    finally:
        await upgraded.close()
        await untouched.close()

    reopened = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    await reopened.initialize()
    try:
        assert reopened.cache_generation == upgraded_generation
        assert await reopened.get_event(room_id, "$standalone") == standalone_event
        assert await reopened.get_thread_events(room_id, thread_id) is None
    finally:
        await reopened.close()

    db = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        trust_row = await (
            await db.execute(
                """
                SELECT value
                FROM mindroom_event_cache_namespace_metadata
                WHERE namespace = %s AND key = %s
                """,
                (namespace, THREAD_HISTORY_TRUST_METADATA_KEY),
            )
        ).fetchone()
        assert trust_row == (THREAD_HISTORY_TRUST_VERSION,)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_opaque_history_trust_upgrade_rolls_back_on_cancellation(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed real PostgreSQL startup must preserve the legacy snapshot and generation."""
    namespace = f"trust_upgrade_cancel_{uuid.uuid4().hex}"
    room_id = "!room:localhost"
    thread_id = "$root"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="root",
        origin_server_ts=1000,
    )
    seeded = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    await seeded.initialize()
    try:
        await _replace_thread(seeded, room_id, thread_id, [root_event], validated_at=100.0)
        original_generation = seeded.cache_generation
    finally:
        await seeded.close()

    db = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        await db.execute(
            """
            DELETE FROM mindroom_event_cache_namespace_metadata
            WHERE namespace = %s AND key = %s
            """,
            (namespace, THREAD_HISTORY_TRUST_METADATA_KEY),
        )
        await db.commit()
    finally:
        await db.close()

    cancel_reason = "maintenance cancelled"

    async def cancel_maintenance(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    monkeypatch.setattr("mindroom.matrix.cache.postgres_event_cache.run_startup_maintenance", cancel_maintenance)
    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await _initialize_postgres_event_cache_db(postgres_event_cache_url, namespace=namespace)

    db = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    try:
        trust_row = await (
            await db.execute(
                """
                SELECT value
                FROM mindroom_event_cache_namespace_metadata
                WHERE namespace = %s AND key = %s
                """,
                (namespace, THREAD_HISTORY_TRUST_METADATA_KEY),
            )
        ).fetchone()
        generation_row = await (
            await db.execute(
                """
                SELECT value
                FROM mindroom_event_cache_namespace_metadata
                WHERE namespace = %s AND key = 'certification_generation'
                """,
                (namespace,),
            )
        ).fetchone()
        snapshot_rows = await (
            await db.execute(
                """
                SELECT event_id
                FROM mindroom_event_cache_thread_events
                WHERE namespace = %s AND room_id = %s AND thread_id = %s
                """,
                (namespace, room_id, thread_id),
            )
        ).fetchall()
        state_row = await (
            await db.execute(
                """
                SELECT validated_at
                FROM mindroom_event_cache_thread_state
                WHERE namespace = %s AND room_id = %s AND thread_id = %s
                """,
                (namespace, room_id, thread_id),
            )
        ).fetchone()
        assert trust_row is None
        assert generation_row == (original_generation,)
        assert snapshot_rows == [(thread_id,)]
        assert state_row == (100.0,)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_shared_disable_covers_current_and_future_principal_views() -> None:
    """A fatal shared-service disable must stop every principal namespace."""
    root = PostgresEventCache(
        database_url="postgresql://cache:test@localhost/mindroom",
        namespace="runtime",
    )
    alice = root.for_principal("@alice:localhost")

    root.disable("fatal schema mismatch")
    bob = root.for_principal("@bob:localhost")
    await asyncio.gather(alice.initialize(), bob.initialize())

    assert root.durable_writes_available is False
    assert alice.durable_writes_available is False
    assert bob.durable_writes_available is False
    assert alice.is_initialized is False
    assert bob.is_initialized is False


@pytest.mark.asyncio
async def test_postgres_event_cache_operation_rolls_back_cancelled_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation inside a Postgres cache operation must not leave the shared connection in a transaction."""
    cache = PostgresEventCache(database_url="postgresql://cache:test@localhost/mindroom", namespace="tenant-a")
    cancel_reason = "stop requested"
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def acquire_db_operation(
        *,
        operation: str,
    ) -> AsyncIterator[object]:
        assert operation == "cancelled_callback"
        yield db

    monkeypatch.setattr(
        cache,
        "_runtime",
        SimpleNamespace(
            is_disabled=False,
            namespace="tenant-a",
            acquire_db_operation=acquire_db_operation,
            has_pending_principal_purge=False,
            is_room_departed=Mock(return_value=False),
        ),
    )
    monkeypatch.setattr(cache, "_flush_pending_writes", AsyncMock(return_value=_FlushedPendingWrites()))
    monkeypatch.setattr(
        postgres_event_cache_threads,
        "load_room_membership_locked",
        AsyncMock(return_value=("joined", 0)),
    )

    async def cancelled_callback(_db: object) -> None:
        raise asyncio.CancelledError(cancel_reason)

    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        await cache._operation(
            "!room:example.test",
            operation="cancelled_callback",
            disabled_result=None,
            callback=cancelled_callback,
        )

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_runtime_rolls_back_cancelled_advisory_lock() -> None:
    """Cancellation while acquiring the advisory lock must rollback the implicit transaction."""
    cancel_reason = "lock wait cancelled"
    runtime = _PostgresEventCacheRuntime(
        "postgresql://cache:test@localhost/mindroom",
        namespace="tenant-a",
    )
    db = SimpleNamespace(
        closed=False,
        execute=AsyncMock(side_effect=asyncio.CancelledError(cancel_reason)),
        rollback=AsyncMock(),
    )
    runtime._db = db

    with pytest.raises(asyncio.CancelledError, match=cancel_reason):
        async with runtime.acquire_db_operation(operation="advisory_lock"):
            pytest.fail("acquire_db_operation should not yield when advisory lock acquisition is cancelled")

    db.rollback.assert_awaited_once()


def test_build_event_cache_auto_installs_postgres_extra_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime factory should satisfy psycopg before importing the Postgres backend."""
    runtime_paths = _runtime_paths(
        tmp_path,
        env={
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:test@localhost/mindroom",
            "MINDROOM_NAMESPACE": "tenant-a",
        },
    )
    install_calls: list[tuple[list[str], str, RuntimePaths]] = []

    class FakePostgresEventCache:
        def __init__(self, *, database_url: str, namespace: str) -> None:
            self.database_url = database_url
            self.namespace = namespace

    def fake_ensure_optional_deps(
        dependencies: list[str],
        extra_name: str,
        runtime_paths_arg: RuntimePaths,
    ) -> None:
        install_calls.append((dependencies, extra_name, runtime_paths_arg))

    def fake_import_module(module_name: str) -> SimpleNamespace:
        assert install_calls == [(["psycopg"], "postgres", runtime_paths)]
        assert module_name == "mindroom.matrix.cache.postgres_event_cache"
        return SimpleNamespace(PostgresEventCache=FakePostgresEventCache)

    monkeypatch.setattr("mindroom.runtime_support.ensure_optional_deps", fake_ensure_optional_deps)
    monkeypatch.setattr("mindroom.runtime_support.import_module", fake_import_module)

    cache = _build_event_cache(CacheConfig(backend="postgres"), runtime_paths)

    assert isinstance(cache, FakePostgresEventCache)
    assert cache.database_url == "postgresql://cache:test@localhost/mindroom"
    assert cache.namespace == "tenant-a"


@pytest.mark.asyncio
async def test_postgres_event_cache_round_trips_core_conversation_cache_behavior(
    postgres_event_cache_url: str,
) -> None:
    """The Postgres backend should preserve the same durable cache semantics as SQLite."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    sender = "@mindroom_agent:localhost"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="thread root",
        origin_server_ts=1000,
    )
    reply_event = _message_event(
        event_id="$reply",
        sender=sender,
        body="first reply",
        origin_server_ts=1010,
        thread_id=thread_id,
    )
    old_edit = _edit_event(
        event_id="$edit-old",
        sender=sender,
        original_event_id="$reply",
        body="old edit",
        origin_server_ts=1020,
    )
    latest_edit = _edit_event(
        event_id="$edit-latest",
        sender=sender,
        original_event_id="$reply",
        body="latest edit",
        origin_server_ts=1030,
    )
    namespace = f"tenant_{uuid.uuid4().hex}"
    isolated_namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    shared_cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    isolated_cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=isolated_namespace)

    await cache.initialize()
    await shared_cache.initialize()
    await isolated_cache.initialize()
    try:
        assert cache.durable_writes_available is True

        await _assert_thread_lookup_behavior(
            cache,
            shared_cache,
            isolated_cache,
            room_id=room_id,
            thread_id=thread_id,
            root_event=root_event,
            reply_event=reply_event,
        )
        await _assert_edit_snapshot_and_mxc_behavior(
            cache,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            old_edit=old_edit,
            latest_edit=latest_edit,
        )
        await _assert_staleness_and_redaction_behavior(
            cache,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            latest_edit=latest_edit,
        )
    finally:
        await cache.close()
        await shared_cache.close()
        await isolated_cache.close()


@pytest.mark.asyncio
async def test_postgres_v3_startup_retains_single_latest_edit_index(
    postgres_event_cache_url: str,
) -> None:
    """Schema v3 keeps one narrowing index while query collation owns correctness."""
    schema_name = f"cache_migration_v3_{uuid.uuid4().hex}"
    isolated_url = await _seed_postgres_v3_schema(postgres_event_cache_url, schema_name)
    cache = PostgresEventCache(database_url=isolated_url, namespace="runtime")
    await cache.initialize()
    try:
        assert "cache_schema_migrated_from" not in cache.runtime_diagnostics()
        assert cache._runtime.db is not None
        version_cursor = await cache._runtime.db.execute(
            "SELECT value FROM mindroom_event_cache_metadata WHERE key = 'schema_version'",
        )
        version = await version_cursor.fetchone()
        indexes_cursor = await cache._runtime.db.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname LIKE 'idx_mindroom_event_cache_event_edits_room_original_ts%'
            ORDER BY indexname
            """,
        )
        indexes = await indexes_cursor.fetchall()
    finally:
        await cache.close()

    assert version == ("3",)
    assert indexes == [("idx_mindroom_event_cache_event_edits_room_original_ts",)]


@pytest.mark.asyncio
async def test_postgres_latest_edit_query_uses_bytewise_event_id_collation(
    postgres_event_cache_url: str,
) -> None:
    """The production lookup must override a non-bytewise event-ID column collation."""
    schema_name = f"collation_{uuid.uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
    await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    await admin.commit()
    await admin.close()

    room_id = "!room:localhost"
    sender = "@alice:localhost"
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(
        database_url=_postgres_schema_url(postgres_event_cache_url, schema_name),
        namespace=namespace,
    )
    await cache.initialize()
    try:
        assert cache._runtime.db is not None
        await cache._runtime.db.execute(
            """
            ALTER TABLE mindroom_event_cache_event_edits
            ALTER COLUMN edit_event_id TYPE TEXT COLLATE "und-x-icu"
            """,
        )
        await cache._runtime.db.commit()
        upper_edit = _edit_event(
            event_id="$Z",
            sender=sender,
            original_event_id="$original",
            body="Upper",
            origin_server_ts=2000,
        )
        lower_edit = _edit_event(
            event_id="$a",
            sender=sender,
            original_event_id="$original",
            body="Lower",
            origin_server_ts=2000,
        )
        await cache.store_events_batch(
            [
                ("$Z", room_id, upper_edit),
                ("$a", room_id, lower_edit),
            ],
        )
        cursor = await cache._runtime.db.execute(
            """
            SELECT edit_event_id
            FROM mindroom_event_cache_event_edits
            WHERE namespace = %s
              AND room_id = %s
              AND original_event_id = '$original'
            ORDER BY origin_server_ts DESC, edit_event_id DESC
            LIMIT 1
            """,
            (namespace, room_id),
        )
        locale_winner = await cursor.fetchone()
        latest_edit = await cache.get_latest_edit(
            room_id,
            "$original",
            sender=sender,
            event_type="m.room.message",
        )
    finally:
        await cache.close()

    assert locale_winner == ("$Z",)
    assert latest_edit is not None
    assert latest_edit["event_id"] == "$a"


@pytest.mark.asyncio
async def test_sqlite_cache_rejects_raw_payload_identity_poison(tmp_path: Path) -> None:
    """SQLite must reject thread poison and filter recent poison before LIMIT."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    cache = SqliteEventCache(tmp_path / "event-cache.db")
    await cache.initialize()
    try:
        reply, poisoned_recent = await _seed_payload_identity_rows(
            cache,
            room_id=room_id,
            thread_id=thread_id,
        )
        assert cache._runtime.db is not None
        await cache._runtime.db.executemany(
            """
            UPDATE events
            SET event_json = ?
            WHERE principal_id = ? AND room_id = ? AND event_id = ?
            """,
            [
                (
                    json.dumps({**reply, "event_id": "$forged-reply"}),
                    cache.principal_id,
                    room_id,
                    "$reply",
                ),
                (
                    json.dumps({**poisoned_recent, "event_id": "$forged-recent"}),
                    cache.principal_id,
                    room_id,
                    "$poisoned-recent",
                ),
            ],
        )
        await cache._runtime.db.commit()
        await _assert_payload_identity_poison_is_rejected(
            cache,
            room_id=room_id,
            thread_id=thread_id,
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_cache_rejects_raw_payload_identity_poison(
    postgres_event_cache_url: str,
) -> None:
    """PostgreSQL must reject thread poison and filter recent poison before LIMIT."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(
        database_url=postgres_event_cache_url,
        namespace=namespace,
    )
    await cache.initialize()
    try:
        reply, poisoned_recent = await _seed_payload_identity_rows(
            cache,
            room_id=room_id,
            thread_id=thread_id,
        )
        assert cache._runtime.db is not None
        for event_id, poisoned_payload in (
            ("$reply", {**reply, "event_id": "$forged-reply"}),
            ("$poisoned-recent", {**poisoned_recent, "event_id": "$forged-recent"}),
        ):
            await cache._runtime.db.execute(
                """
                UPDATE mindroom_event_cache_events
                SET event_json = %s
                WHERE namespace = %s AND room_id = %s AND event_id = %s
                """,
                (json.dumps(poisoned_payload), namespace, room_id, event_id),
            )
        await cache._runtime.db.commit()
        await _assert_payload_identity_poison_is_rejected(
            cache,
            room_id=room_id,
            thread_id=thread_id,
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_recovers_after_backend_connection_termination(
    postgres_event_cache_url: str,
) -> None:
    """A transient Postgres disconnect should reconnect instead of disabling cache."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="thread root",
        origin_server_ts=1000,
    )
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)

    await cache.initialize()
    try:
        await _replace_thread(cache, room_id, thread_id, [root_event], validated_at=100.0)
        assert cache._runtime.db is not None
        cursor = await cache._runtime.db.execute("SELECT pg_backend_pid()")
        row = await cursor.fetchone()
        assert row is not None
        pid = int(row[0])

        admin = await psycopg.AsyncConnection.connect(postgres_event_cache_url)
        try:
            terminate_cursor = await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            terminate_row = await terminate_cursor.fetchone()
            assert terminate_row == (True,)
            await admin.commit()
        finally:
            await admin.close()

        await cache.mark_thread_stale(room_id, thread_id, reason="live_thread_mutation")

        state = await cache.get_thread_cache_state(room_id, thread_id)
        assert state is not None
        assert state.invalidated_at is not None
        assert state.invalidation_reason == "live_thread_mutation"
        assert cache.durable_writes_available is True
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_postgres_disabled"] is False
        assert diagnostics["cache_postgres_reconnect_count"] >= 1
        assert diagnostics["cache_postgres_transient_failure_count"] >= 1
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_flushes_pending_invalidations_before_guarded_replace(
    postgres_event_cache_url: str,
) -> None:
    """A deferred stale marker must be persisted before a guarded cache replacement can win."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="thread root",
        origin_server_ts=1000,
    )
    replacement_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="replacement root",
        origin_server_ts=2000,
    )
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)

    await cache.initialize()
    try:
        await _replace_thread(cache, room_id, thread_id, [root_event], validated_at=100.0)
        cache._runtime.record_pending_thread_invalidation(
            room_id,
            thread_id,
            invalidated_at=200.0,
            reason="live_thread_mutation",
        )

        replaced = await cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            [replacement_event],
            expected_membership_epoch=await cache.room_membership_epoch(room_id),
            fetch_started_at=150.0,
        )

        assert replaced is False
        state = await cache.get_thread_cache_state(room_id, thread_id)
        assert state is not None
        assert state.invalidated_at == 200.0
        assert state.invalidation_reason == "live_thread_mutation"
        assert cache.runtime_diagnostics()["cache_postgres_pending_thread_invalidations"] == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_flushes_newer_thread_marker_with_pending_room_marker(
    postgres_event_cache_url: str,
) -> None:
    """A pending room marker must not hide a newer pending thread marker."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="thread root",
        origin_server_ts=1000,
    )
    replacement_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="replacement root",
        origin_server_ts=2000,
    )
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)

    await cache.initialize()
    try:
        await _replace_thread(cache, room_id, thread_id, [root_event], validated_at=50.0)
        cache._runtime.record_pending_room_invalidation(
            room_id,
            invalidated_at=100.0,
            reason="unknown_room_mutation",
        )
        cache._runtime.record_pending_thread_invalidation(
            room_id,
            thread_id,
            invalidated_at=200.0,
            reason="live_thread_mutation",
        )

        replaced = await cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            [replacement_event],
            expected_membership_epoch=await cache.room_membership_epoch(room_id),
            fetch_started_at=150.0,
        )

        assert replaced is False
        state = await cache.get_thread_cache_state(room_id, thread_id)
        assert state is not None
        assert state.room_invalidated_at == 100.0
        assert state.invalidated_at == 200.0
        assert state.invalidation_reason == "live_thread_mutation"
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_postgres_pending_room_invalidations"] == 0
        assert diagnostics["cache_postgres_pending_thread_invalidations"] == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_preserves_pending_marker_recorded_during_flush(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker recorded while an older marker flushes must remain pending for a later operation."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    root_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="thread root",
        origin_server_ts=1000,
    )
    replacement_event = _message_event(
        event_id=thread_id,
        sender="@user:localhost",
        body="replacement root",
        origin_server_ts=2000,
    )
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    original_mark_thread_stale_locked = postgres_event_cache_threads.mark_thread_stale_locked
    injected_newer_pending_marker = False

    async def racing_mark_thread_stale_locked(*args: object, **kwargs: object) -> None:
        nonlocal injected_newer_pending_marker
        if kwargs.get("thread_id") == thread_id and kwargs.get("invalidated_at") == 200.0:
            injected_newer_pending_marker = True
            cache._runtime.record_pending_thread_invalidation(
                room_id,
                thread_id,
                invalidated_at=300.0,
                reason="later_live_thread_mutation",
            )
        await original_mark_thread_stale_locked(*args, **kwargs)

    monkeypatch.setattr(
        postgres_event_cache_threads,
        "mark_thread_stale_locked",
        racing_mark_thread_stale_locked,
    )

    await cache.initialize()
    try:
        await _replace_thread(cache, room_id, thread_id, [root_event], validated_at=50.0)
        membership_epoch = await cache.room_membership_epoch(room_id)
        cache._runtime.record_pending_thread_invalidation(
            room_id,
            thread_id,
            invalidated_at=200.0,
            reason="live_thread_mutation",
        )

        replaced = await cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            [replacement_event],
            expected_membership_epoch=membership_epoch,
            fetch_started_at=150.0,
        )

        assert replaced is False
        assert injected_newer_pending_marker is True
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_postgres_pending_thread_invalidations"] == 1

        state = await cache.get_thread_cache_state(room_id, thread_id)

        assert state is not None
        assert state.invalidated_at == 300.0
        assert state.invalidation_reason == "later_live_thread_mutation"
        diagnostics = cache.runtime_diagnostics()
        assert diagnostics["cache_postgres_pending_thread_invalidations"] == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_pending_thread_flush_does_not_downgrade_newer_durable_marker(
    postgres_event_cache_url: str,
) -> None:
    """An older pending thread marker must not overwrite a newer durable invalidation."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)

    await cache.initialize()
    try:
        cache._runtime.record_pending_thread_invalidation(
            room_id,
            thread_id,
            invalidated_at=100.0,
            reason="older_pending_marker",
        )
        async with cache._runtime.acquire_db_operation(operation="test_newer_thread_marker") as db:
            await postgres_event_cache_threads.mark_thread_stale_locked(
                db,
                namespace=namespace,
                room_id=room_id,
                thread_id=thread_id,
                invalidated_at=200.0,
                reason="newer_durable_marker",
            )
            await db.commit()

        await cache.flush_pending_durable_writes(room_id)
        state = await cache.get_thread_cache_state(room_id, thread_id)

        assert state is not None
        assert state.invalidated_at == 200.0
        assert state.invalidation_reason == "newer_durable_marker"
        assert cache.pending_durable_write_room_ids() == ()
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_postgres_event_cache_pending_room_flush_does_not_downgrade_newer_durable_marker(
    postgres_event_cache_url: str,
) -> None:
    """An older pending room marker must not overwrite a newer durable invalidation."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    namespace = f"tenant_{uuid.uuid4().hex}"
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)

    await cache.initialize()
    try:
        cache._runtime.record_pending_room_invalidation(
            room_id,
            invalidated_at=100.0,
            reason="older_pending_room_marker",
        )
        async with cache._runtime.acquire_db_operation(operation="test_newer_room_marker") as db:
            await postgres_event_cache_threads.mark_room_stale_locked(
                db,
                namespace=namespace,
                room_id=room_id,
                invalidated_at=200.0,
                reason="newer_durable_room_marker",
            )
            await db.commit()

        await cache.flush_pending_durable_writes(room_id)
        state = await cache.get_thread_cache_state(room_id, thread_id)

        assert state is not None
        assert state.room_invalidated_at == 200.0
        assert state.room_invalidation_reason == "newer_durable_room_marker"
        assert cache.pending_durable_write_room_ids() == ()
    finally:
        await cache.close()


def test_postgres_transient_classifier_accepts_startup_connection_refused() -> None:
    """Startup connection-refused errors should retry later instead of disabling the cache."""
    assert _is_transient_postgres_failure(psycopg.OperationalError("connection failed: Connection refused"))


def test_postgres_transient_classifier_accepts_connection_timeout() -> None:
    """Startup connection timeouts should retry later instead of disabling the cache."""
    assert _is_transient_postgres_failure(psycopg.errors.ConnectionTimeout("connection timeout expired"))


def test_postgres_transient_classifier_accepts_dns_resolution_failure() -> None:
    """Transient Kubernetes DNS gaps should retry later instead of disabling the cache."""
    assert _is_transient_postgres_failure(
        psycopg.OperationalError(
            "failed to resolve host 'mindroom-cache-postgres': [Errno -2] Name or service not known",
        ),
    )


def test_postgres_transient_classifier_rejects_authentication_failures_without_sqlstate() -> None:
    """Authentication failures should disable the cache instead of retrying forever."""
    exc = psycopg.OperationalError(
        'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
        'FATAL: password authentication failed for user "cache"',
    )

    assert not _is_transient_postgres_failure(exc)


def test_postgres_pending_invalidation_records_are_monotonic() -> None:
    """Pending invalidation buffers should keep the newest marker for each scope."""
    runtime = _PostgresEventCacheRuntime("postgresql://cache:secret@db.internal/mindroom", namespace="tenant")

    runtime.record_pending_thread_invalidation(
        "!room:localhost",
        "$thread",
        invalidated_at=200.0,
        reason="newer_thread_marker",
    )
    runtime.record_pending_thread_invalidation(
        "!room:localhost",
        "$thread",
        invalidated_at=100.0,
        reason="older_thread_marker",
    )
    runtime.record_pending_room_invalidation(
        "!room:localhost",
        invalidated_at=200.0,
        reason="newer_room_marker",
    )
    runtime.record_pending_room_invalidation(
        "!room:localhost",
        invalidated_at=100.0,
        reason="older_room_marker",
    )
    runtime.record_pending_thread_invalidation(
        "!room:localhost",
        "$strong-thread",
        invalidated_at=100.0,
        reason="sync_opaque_encrypted_event",
    )
    runtime.record_pending_thread_invalidation(
        "!room:localhost",
        "$strong-thread",
        invalidated_at=300.0,
        reason="sync_thread_mutation",
    )

    thread_pending = runtime.pending_thread_invalidations("!room:localhost")
    room_pending = runtime.pending_room_invalidation("!room:localhost")

    pending_by_thread = dict(thread_pending)
    assert pending_by_thread["$thread"].invalidated_at == 200.0
    assert pending_by_thread["$thread"].reason == "newer_thread_marker"
    assert pending_by_thread["$strong-thread"].invalidated_at == 300.0
    assert pending_by_thread["$strong-thread"].reason == "sync_opaque_encrypted_event"
    assert room_pending is not None
    assert room_pending.invalidated_at == 200.0
    assert room_pending.reason == "newer_room_marker"


@pytest.mark.asyncio
async def test_event_cache_startup_backend_unavailable_retries_without_disabling(tmp_path: Path) -> None:
    """A transient startup outage should leave the cache enabled for the next sync retry."""
    runtime_paths = _runtime_paths(tmp_path)
    cache_config = CacheConfig()
    cache = Mock()
    cache.is_initialized = False
    cache.initialize = AsyncMock()
    cache.disable = Mock()
    initialize_attempts = 0

    async def initialize() -> None:
        nonlocal initialize_attempts
        initialize_attempts += 1
        if initialize_attempts == 1:
            reason = "postgres unavailable"
            raise EventCacheBackendUnavailableError(reason)
        cache.is_initialized = True

    cache.initialize.side_effect = initialize
    logger = get_logger("tests.event_cache_backends")
    support = OwnedRuntimeSupport(
        event_cache=cast("ConversationEventCache", cache),
        event_cache_write_coordinator=EventCacheWriteCoordinator(logger=logger),
        startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        event_cache_identity=_event_cache_runtime_identity(cache_config, runtime_paths),
    )

    await _initialize_event_cache_best_effort(
        support,
        logger=logger,
        init_failure_reason_prefix="test_startup",
    )

    assert initialize_attempts == 1
    assert cache.is_initialized is False
    cache.disable.assert_not_called()

    retried_support = await sync_owned_runtime_support(
        support,
        cache_config=cache_config,
        runtime_paths=runtime_paths,
        logger=logger,
        background_task_owner=object(),
        init_failure_reason_prefix="test_retry",
        log_db_path_change=False,
    )

    assert retried_support is support
    assert initialize_attempts == 2
    assert cache.is_initialized is True
    cache.disable.assert_not_called()


def test_build_event_cache_requires_postgres_database_url(tmp_path: Path) -> None:
    """Postgres should fail during cache construction when no database URL is available."""
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(ValueError, match="MINDROOM_EVENT_CACHE_DATABASE_URL"):
        _build_event_cache(CacheConfig(backend="postgres"), runtime_paths)


@pytest.mark.parametrize(
    ("conninfo", "expected"),
    [
        (
            "postgresql://cache:secret@db.internal/mindroom",
            "postgresql://***@db.internal/mindroom",
        ),
        (
            "postgresql://db.internal/mindroom?user=cache&password=secret&sslmode=require",
            "postgresql://db.internal/mindroom?user=cache&password=***&sslmode=require",
        ),
        (
            "host=db.internal user=cache password='secret value' dbname=mindroom",
            "host=db.internal user=cache password=*** dbname=mindroom",
        ),
    ],
)
def test_postgres_connection_info_redaction_hides_secret_forms(conninfo: str, expected: str) -> None:
    """Postgres connection redaction should cover URL and libpq secret forms."""
    runtime = _PostgresEventCacheRuntime(conninfo, namespace="tenant-a")

    assert runtime.redacted_database_url == expected


def test_event_cache_runtime_identity_uses_shared_postgres_redaction() -> None:
    """Runtime backend-change logs should use the same Postgres redaction policy."""
    identity = _EventCacheRuntimeIdentity(
        backend="postgres",
        location="postgresql://db.internal/mindroom?user=cache&password=secret",
        namespace="tenant-a",
    )

    assert identity.redacted_location == "postgresql://db.internal/mindroom?user=cache&password=***"
