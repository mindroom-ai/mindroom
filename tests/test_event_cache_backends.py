"""Runtime selection for Matrix event-cache backends."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest

from mindroom.config.matrix import CacheConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.logging_config import get_logger
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.postgres_event_cache import (
    PostgresEventCache,
    PostgresEventCacheRuntime,
    _is_transient_postgres_failure,
)
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.runtime_support import (
    EventCacheRuntimeIdentity,
    OwnedRuntimeSupport,
    StartupThreadPrewarmRegistry,
    build_event_cache,
    event_cache_runtime_identity,
    initialize_event_cache_best_effort,
    sync_owned_runtime_support,
)

if TYPE_CHECKING:
    from pathlib import Path

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
    await cache.replace_thread(room_id, thread_id, [reply_event, root_event], validated_at=100.0)

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
    assert await cache.get_latest_edit(room_id, "$reply") == latest_edit
    snapshot = await cache.get_latest_agent_message_snapshot(
        room_id,
        thread_id,
        sender,
        runtime_started_at=None,
    )
    assert snapshot is not None
    assert snapshot.content["body"] == "latest edit"
    assert snapshot.origin_server_ts == 1030

    await cache.store_mxc_text(room_id, "mxc://localhost/media", "downloaded text")
    assert await cache.get_mxc_text(room_id, "mxc://localhost/media") == "downloaded text"


async def _assert_staleness_and_redaction_behavior(
    cache: PostgresEventCache,
    *,
    room_id: str,
    thread_id: str,
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
    assert await cache.get_latest_edit(room_id, "$reply") is None
    redacted_thread = await cache.get_thread_events(room_id, thread_id)
    assert redacted_thread is not None
    assert [event["event_id"] for event in redacted_thread] == [thread_id]

    await cache.store_event("$edit-latest", room_id, latest_edit)
    assert await cache.get_latest_edit(room_id, "$reply") is None


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
    cache = build_event_cache(CacheConfig(), runtime_paths)

    assert isinstance(cache, SqliteEventCache)
    assert cache.db_path == tmp_path / "mindroom_data" / "event_cache.db"


def test_build_event_cache_uses_postgres_when_configured(tmp_path: Path) -> None:
    """The runtime factory should construct the Postgres cache backend only when requested."""
    runtime_paths = _runtime_paths(
        tmp_path,
        env={
            "MINDROOM_EVENT_CACHE_DATABASE_URL": "postgresql://cache:test@localhost/mindroom",
            "MINDROOM_NAMESPACE": "tenant-a",
        },
    )
    cache = build_event_cache(CacheConfig(backend="postgres"), runtime_paths)

    assert isinstance(cache, PostgresEventCache)
    assert cache.database_url == "postgresql://cache:test@localhost/mindroom"
    assert cache.namespace == "tenant-a"


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

    cache = build_event_cache(CacheConfig(backend="postgres"), runtime_paths)

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
            latest_edit=latest_edit,
        )
    finally:
        await cache.close()
        await shared_cache.close()
        await isolated_cache.close()


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
        await cache.replace_thread(room_id, thread_id, [root_event], validated_at=100.0)
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
        await cache.replace_thread(room_id, thread_id, [root_event], validated_at=100.0)
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
        await cache.replace_thread(room_id, thread_id, [root_event], validated_at=50.0)
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


def test_postgres_transient_classifier_accepts_startup_connection_refused() -> None:
    """Startup connection-refused errors should retry later instead of disabling the cache."""
    assert _is_transient_postgres_failure(psycopg.OperationalError("connection failed: Connection refused"))


def test_postgres_transient_classifier_rejects_authentication_failures_without_sqlstate() -> None:
    """Authentication failures should disable the cache instead of retrying forever."""
    exc = psycopg.OperationalError(
        'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
        'FATAL: password authentication failed for user "cache"',
    )

    assert not _is_transient_postgres_failure(exc)


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
        event_cache_write_coordinator=_EventCacheWriteCoordinator(logger=logger),
        startup_thread_prewarm_registry=StartupThreadPrewarmRegistry(),
        event_cache_identity=event_cache_runtime_identity(cache_config, runtime_paths),
    )

    await initialize_event_cache_best_effort(
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
        build_event_cache(CacheConfig(backend="postgres"), runtime_paths)


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
    runtime = PostgresEventCacheRuntime(conninfo, namespace="tenant-a")

    assert runtime.redacted_database_url == expected


def test_event_cache_runtime_identity_uses_shared_postgres_redaction() -> None:
    """Runtime backend-change logs should use the same Postgres redaction policy."""
    identity = EventCacheRuntimeIdentity(
        backend="postgres",
        location="postgresql://db.internal/mindroom?user=cache&password=secret",
        namespace="tenant-a",
    )

    assert identity.redacted_location == "postgresql://db.internal/mindroom?user=cache&password=***"
