"""Recovery behavior of the Postgres event cache after transient connection loss."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
import pytest_asyncio

from mindroom.matrix.cache.postgres_event_cache import (
    PostgresEventCache,
    _is_transient_db_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


pytestmark = pytest.mark.asyncio


def _build_event(event_id: str, thread_id: str, ts: int) -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@alice:localhost",
        "origin_server_ts": ts,
        "content": {
            "msgtype": "m.text",
            "body": event_id,
            "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
        },
    }


@pytest_asyncio.fixture
async def postgres_event_cache(postgres_event_cache_url: str) -> AsyncIterator[PostgresEventCache]:
    """Yield a fresh PostgresEventCache rooted at a unique namespace."""
    namespace = uuid.uuid4().hex
    cache = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    await cache.initialize()
    try:
        yield cache
    finally:
        await cache.close()


async def test_is_transient_db_error_classifies_admin_shutdown() -> None:
    """Admin-shutdown style errors should be classified as transient via message match."""
    err = psycopg.OperationalError(
        "terminating connection due to administrator command",
    )
    assert _is_transient_db_error(err)


async def test_is_transient_db_error_classifies_typed_psycopg_errors() -> None:
    """Real psycopg typed errors should be classified by their ``sqlstate`` attribute."""
    assert _is_transient_db_error(psycopg.errors.AdminShutdown("admin"))
    assert _is_transient_db_error(psycopg.errors.CrashShutdown("crash"))
    assert _is_transient_db_error(psycopg.errors.CannotConnectNow("starting"))
    assert _is_transient_db_error(psycopg.errors.ConnectionFailure("conn fail"))
    assert not _is_transient_db_error(psycopg.errors.SyntaxError("bad sql"))


async def test_is_transient_db_error_classifies_sqlstate_via_diag() -> None:
    """Errors that only expose ``diag.sqlstate`` (no top-level attr) should still classify."""

    class _FakeDiag:
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    class _FakeOperationalError(psycopg.OperationalError):
        def __init__(self, sqlstate: str) -> None:
            super().__init__("connection closed")
            self._fake_diag = _FakeDiag(sqlstate)

        @property
        def diag(self) -> _FakeDiag:  # type: ignore[override]
            return self._fake_diag

    assert _is_transient_db_error(_FakeOperationalError("57P01"))
    assert _is_transient_db_error(_FakeOperationalError("08006"))
    assert not _is_transient_db_error(_FakeOperationalError("42601"))


async def test_is_transient_db_error_classifies_interface_error() -> None:
    """``InterfaceError`` raised on a closed connection should be classified as transient."""
    assert _is_transient_db_error(psycopg.InterfaceError("the connection is closed"))


async def test_is_transient_db_error_rejects_logic_errors() -> None:
    """Programming-style errors should not be retried."""
    assert not _is_transient_db_error(psycopg.ProgrammingError("syntax error"))
    assert not _is_transient_db_error(ValueError("not a db error"))


async def test_invalidate_thread_recovers_after_connection_close(
    postgres_event_cache: PostgresEventCache,
) -> None:
    """A force-closed connection during a write must reconnect and complete the operation."""
    room_id = "!recovery:localhost"
    thread_id = "$thread_recovery:localhost"
    await postgres_event_cache.replace_thread(
        room_id,
        thread_id,
        events=[_build_event("$ev1", thread_id, ts=1)],
        validated_at=1.0,
    )
    cached_before = await postgres_event_cache.get_thread_events(room_id, thread_id)
    assert cached_before is not None
    assert len(cached_before) == 1

    db = postgres_event_cache._runtime.require_db()
    await db.close()

    await postgres_event_cache.invalidate_thread(room_id, thread_id)

    assert postgres_event_cache.disabled_reason is None
    assert postgres_event_cache.reconnect_attempts == 1
    assert postgres_event_cache.unavailable_reason is not None

    cached_after = await postgres_event_cache.get_thread_events(room_id, thread_id)
    assert cached_after is None or cached_after == []


async def test_read_after_connection_close_recovers(
    postgres_event_cache: PostgresEventCache,
) -> None:
    """Reads should also reconnect on transient connection failures."""
    room_id = "!read_recovery:localhost"
    thread_id = "$thread_read_recovery:localhost"
    await postgres_event_cache.replace_thread(
        room_id,
        thread_id,
        events=[_build_event("$ev_r1", thread_id, ts=1)],
        validated_at=1.0,
    )

    db = postgres_event_cache._runtime.require_db()
    await db.close()

    events = await postgres_event_cache.get_thread_events(room_id, thread_id)
    assert events is not None
    assert len(events) == 1
    assert postgres_event_cache.disabled_reason is None
    assert postgres_event_cache.reconnect_attempts == 1
