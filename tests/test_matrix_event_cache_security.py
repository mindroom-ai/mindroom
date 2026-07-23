"""Security and plaintext-lifecycle contract tests for every durable cache backend."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix import client_thread_history
from mindroom.matrix.cache import (
    ConversationEventCache,
    SharedConversationEventCache,
    postgres_event_cache_events,
    postgres_event_cache_threads,
    sqlite_event_cache,
    sqlite_event_cache_events,
    sqlite_event_cache_threads,
)
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_cache_invalidation import mark_thread_stale_fail_closed
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.rooms import leave_non_dm_rooms
from mindroom.matrix.sync_cache_trust import SyncCacheTrust
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from aiosqlite import Cursor


def _sidecar_content(mxc_url: str, *, encrypted: bool) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": "preview",
        "msgtype": "m.file",
        "io.mindroom.long_text": {
            "version": 2,
            "encoding": "matrix_event_content_json",
        },
    }
    if encrypted:
        content["file"] = {"url": mxc_url, "key": {"k": "secret"}}
    else:
        content["url"] = mxc_url
    return content


def _event(
    event_id: str,
    timestamp: int,
    *,
    body: str = "message",
    sidecar_url: str | None = None,
    encrypted: bool = False,
    sidecar_in_new_content: bool = False,
    edit_of: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"body": body, "msgtype": "m.text"}
    if sidecar_url is not None:
        sidecar = _sidecar_content(sidecar_url, encrypted=encrypted)
        if sidecar_in_new_content:
            content["m.new_content"] = sidecar
        else:
            content = sidecar
    if edit_of is not None:
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": edit_of}
    return {
        "event_id": event_id,
        "sender": "@agent:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


def _shared_cache(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> SharedConversationEventCache:
    cache = event_cache_factory()
    assert isinstance(cache, SharedConversationEventCache)
    return cast("SharedConversationEventCache", cache)


async def _raw_mxc_text_count(
    cache: ConversationEventCache,
    room_id: str,
    mxc_url: str,
) -> int:
    """Count physical plaintext rows without relying on ownership-filtered public reads."""
    if isinstance(cache, SqliteEventCache):
        async with cache._runtime.acquire_db_operation() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM mxc_text_cache
                WHERE principal_id = ? AND room_id = ? AND mxc_url = ?
                """,
                (cache.principal_id, room_id, mxc_url),
            )
            row = await cursor.fetchone()
            await cursor.close()
    else:
        assert isinstance(cache, PostgresEventCache)
        async with cache._runtime.acquire_db_operation(operation="test_raw_mxc_text_count") as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM mindroom_event_cache_mxc_text
                WHERE namespace = %s AND room_id = %s AND mxc_url = %s
                """,
                (cache.namespace, room_id, mxc_url),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
    assert row is not None
    return int(row[0])


async def _assert_redacted_events_do_not_resurrect(
    event_cache_factory: Callable[[], ConversationEventCache],
    *,
    principal_id: str,
    room_id: str,
    shared_mxc: str,
    dependent_mxc: str,
    events: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """Reopen the backend and prove tombstones and physical plaintext deletion survive."""
    reopened_root = _shared_cache(event_cache_factory)
    await reopened_root.initialize()
    reopened = reopened_root.for_principal(principal_id)
    try:
        assert await _raw_mxc_text_count(reopened, room_id, shared_mxc) == 0
        assert await _raw_mxc_text_count(reopened, room_id, dependent_mxc) == 0
        await reopened.store_events_batch(events)
        for event_id, _event_room_id, _event_data in events:
            assert await reopened.get_event(room_id, event_id) is None
        assert await reopened.store_mxc_text(room_id, "$top", shared_mxc, "late plaintext") is False
        assert await reopened.store_mxc_text(room_id, "$edit", dependent_mxc, "late plaintext") is False
        assert await _raw_mxc_text_count(reopened, room_id, shared_mxc) == 0
        assert await _raw_mxc_text_count(reopened, room_id, dependent_mxc) == 0
    finally:
        await reopened_root.close()


async def _assert_room_purge_survives_restart(
    event_cache_factory: Callable[[], ConversationEventCache],
    *,
    principal_id: str,
    room_id: str,
    event_id: str,
    mxc_url: str,
) -> None:
    """Reopen a backend and prove one departed principal-room remains empty."""
    reopened_root = _shared_cache(event_cache_factory)
    await reopened_root.initialize()
    reopened = reopened_root.for_principal(principal_id)
    try:
        assert await reopened.get_event(room_id, event_id) is None
        assert await _raw_mxc_text_count(reopened, room_id, mxc_url) == 0
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_room_scope_is_part_of_event_and_plaintext_identity(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The same event ID or MXC URL in two rooms must never cross room boundaries."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    mxc_url = "mxc://server/shared-name"
    try:
        room_a_event = _event("$same", 1, body="room A", sidecar_url=mxc_url)
        room_b_event = _event("$same", 2, body="room B", sidecar_url=mxc_url)
        await cache.store_event("$same", "!a:localhost", room_a_event)
        await cache.store_event("$same", "!b:localhost", room_b_event)
        assert await cache.store_mxc_text("!a:localhost", "$same", mxc_url, "plaintext A")
        assert await cache.store_mxc_text("!b:localhost", "$same", mxc_url, "plaintext B")

        assert await cache.get_event("!a:localhost", "$same") == room_a_event
        assert await cache.get_event("!b:localhost", "$same") == room_b_event
        assert await cache.get_event("!wrong:localhost", "$same") is None
        assert await cache.get_mxc_text("!a:localhost", "$same", mxc_url) == "plaintext A"
        assert await cache.get_mxc_text("!b:localhost", "$same", mxc_url) == "plaintext B"
        assert await cache.get_mxc_text("!wrong:localhost", "$same", mxc_url) is None
        room_a_epoch = await cache.room_membership_epoch("!a:localhost")
        assert room_a_epoch is not None
        assert await cache.get_mxc_texts(
            "!a:localhost",
            {("$same", mxc_url), ("$missing", mxc_url)},
            expected_membership_epoch=room_a_epoch,
        ) == {("$same", mxc_url): "plaintext A"}
        assert (
            await cache.get_mxc_texts(
                "!a:localhost",
                {("$same", mxc_url)},
                expected_membership_epoch=room_a_epoch + 1,
            )
            == {}
        )
    finally:
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decrypted_principal_first", [True, False])
async def test_principal_isolation_survives_asymmetric_decryption_and_leave(
    event_cache_factory: Callable[[], ConversationEventCache],
    *,
    decrypted_principal_first: bool,
) -> None:
    """One joined bot's decrypted plaintext must remain invisible to every other bot."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!encrypted:localhost"
    event_id = "$encrypted-sidecar"
    mxc_url = "mxc://server/encrypted"
    alice_event = _event(event_id, 1, sidecar_url=mxc_url, encrypted=True)
    bob_opaque_event = _event(event_id, 1, body="unable to decrypt")
    ordered_writes = (
        ((alice, alice_event), (bob, bob_opaque_event))
        if decrypted_principal_first
        else ((bob, bob_opaque_event), (alice, alice_event))
    )
    try:
        for cache, event in ordered_writes:
            await cache.store_event(event_id, room_id, event)
        assert await alice.store_mxc_text(room_id, event_id, mxc_url, "alice plaintext")
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await bob.store_mxc_text(room_id, event_id, mxc_url, "stolen") is False

        bob_event = _event(event_id, 2, sidecar_url=mxc_url, encrypted=True)
        await bob.store_event(event_id, room_id, bob_event)
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await bob.store_mxc_text(room_id, event_id, mxc_url, "bob plaintext")
        assert await alice.get_mxc_text(room_id, event_id, mxc_url) == "alice plaintext"

        await alice.purge_room(room_id)
        assert await alice.get_event(room_id, event_id) is None
        assert await alice.get_mxc_text(room_id, event_id, mxc_url) is None
        await alice.store_event("$late", room_id, _event("$late", 3, sidecar_url=mxc_url))
        assert await alice.store_mxc_text(room_id, "$late", mxc_url, "late plaintext") is False
        assert await alice.get_event(room_id, "$late") is None
        assert await alice.get_mxc_text(room_id, "$late", mxc_url) is None
        assert await bob.get_event(room_id, event_id) == bob_event
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) == "bob plaintext"

        await alice.mark_room_joined(
            room_id,
            expected_departure_epoch=alice.room_departure_epoch(room_id),
        )
        rejoined_event = _event("$rejoined", 4, sidecar_url=mxc_url)
        await alice.store_event("$rejoined", room_id, rejoined_event)
        assert await alice.store_mxc_text(room_id, "$rejoined", mxc_url, "rejoined plaintext")
        assert await alice.get_event(room_id, "$rejoined") == rejoined_event
        assert await bob.get_mxc_text(room_id, event_id, mxc_url) == "bob plaintext"
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_closing_principal_view_does_not_close_shared_runtime(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A bot stopping must not close cache storage still used by another bot."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    try:
        await alice.store_event("$alice", room_id, _event("$alice", 1))
        await bob.store_event("$bob", room_id, _event("$bob", 2))

        await alice.close()

        assert await bob.get_event(room_id, "$bob") == _event("$bob", 2)
        await bob.store_event("$bob-after-close", room_id, _event("$bob-after-close", 3))
        assert await bob.get_event(room_id, "$bob-after-close") == _event("$bob-after-close", 3)
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_disabling_principal_view_does_not_disable_other_principals(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A principal-scoped safety failure must not take down another bot's cache view."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    alice_event = _event("$alice", 1)
    bob_event = _event("$bob", 2)
    try:
        await alice.store_event("$alice", room_id, alice_event)
        await bob.store_event("$bob", room_id, bob_event)

        alice.disable("principal checkpoint failure")

        assert alice.durable_writes_available is False
        assert await alice.get_event(room_id, "$alice") is None
        assert bob.durable_writes_available is True
        assert await bob.get_event(room_id, "$bob") == bob_event

        root.disable("shared schema failure")

        assert bob.durable_writes_available is False
        assert await bob.get_event(room_id, "$bob") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_sqlite_lock_contention_quarantines_then_heals_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient SQLite writer must fence stale data without disabling the principal forever."""
    root = SqliteEventCache(tmp_path / "event_cache.db")
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    thread_id = "$thread"
    alice_event = _event(thread_id, 1)
    bob_event = _event("$bob", 2)
    try:
        await replace_thread_unconditionally(alice, room_id, thread_id, [alice_event])
        await bob.store_event("$bob", room_id, bob_event)
        db = root._runtime.require_db()
        read_logger = MagicMock()
        monkeypatch.setattr(sqlite_event_cache, "logger", read_logger)
        blocker = sqlite3.connect(root.db_path, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            readable_state = await asyncio.wait_for(
                alice.get_thread_cache_state(room_id, thread_id),
                timeout=0.5,
            )
            assert readable_state is None
            read_logger.debug.assert_called_once_with(
                "SQLite event cache read skipped because another writer owns storage",
                operation="get_thread_cache_state",
            )
            timeout_cursor = await db.execute("PRAGMA busy_timeout")
            assert await timeout_cursor.fetchone() == (5000,)
            await timeout_cursor.close()
            await db.execute("PRAGMA busy_timeout=0")
            await mark_thread_stale_fail_closed(
                alice,
                room_id=room_id,
                thread_id=thread_id,
                reason="outbound_thread_mutation",
                logger=MagicMock(),
            )
        finally:
            blocker.rollback()
            blocker.close()

        diagnostics = alice.runtime_diagnostics()
        assert diagnostics["cache_sqlite_principal_disabled"] is False
        assert diagnostics["cache_sqlite_pending_principal_purge"] is True
        assert diagnostics["cache_sqlite_read_contention_count"] == 1
        assert alice.durable_writes_available is False
        assert alice.cache_generation is None

        assert await alice.get_thread_cache_state(room_id, thread_id) is None
        assert alice.runtime_diagnostics()["cache_sqlite_pending_principal_purge"] is False
        assert await bob.get_event(room_id, "$bob") == bob_event

        await replace_thread_unconditionally(alice, room_id, thread_id, [alice_event])
        healed_state = await alice.get_thread_cache_state(room_id, thread_id)
        assert healed_state is not None
        assert healed_state.validated_at is not None
        assert healed_state.invalidated_at is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_sqlite_pending_principal_purge_does_not_strand_rejoined_room(tmp_path: Path) -> None:
    """A rejoin must flush a pending principal purge before lifting its departure fence."""
    root = SqliteEventCache(tmp_path / "event_cache.db")
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    try:
        await alice.store_event(event_id, room_id, event)
        departure_epoch = alice.mark_room_departed(room_id)
        root._runtime.record_pending_principal_purge(alice.principal_id)

        await alice.mark_room_joined(room_id, expected_departure_epoch=departure_epoch)

        diagnostics = alice.runtime_diagnostics()
        assert diagnostics["cache_sqlite_pending_principal_purge"] is False
        assert diagnostics["cache_sqlite_departed_room_count"] == 0
        assert alice.durable_writes_available is True
        assert await alice.get_event(room_id, event_id) is None
        await alice.store_event(event_id, room_id, event)
        assert await alice.get_event(room_id, event_id) == event
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_failed_room_purge_blocks_reads_until_recovery(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient leave cleanup failure must remain pending and flush before later reads."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    await cache.store_event(event_id, room_id, event)

    if isinstance(cache, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(cache, PostgresEventCache)
        module = postgres_event_cache_events
    original_purge = module.purge_room_locked
    failure_reason = "temporary purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, "purge_room_locked", fail_purge)
        with pytest.raises(RuntimeError, match="temporary purge failure"):
            await cache.purge_room(room_id)
        assert cache.pending_durable_write_room_ids() == (room_id,)

        monkeypatch.setattr(module, "purge_room_locked", original_purge)
        await cache.flush_pending_durable_writes(room_id)
        assert await cache.get_event(room_id, event_id) is None
        assert cache.pending_durable_write_room_ids() == ()
    finally:
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_kind", ["event", "mxc"])
async def test_departure_discards_read_that_started_before_fence(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
    *,
    lookup_kind: str,
) -> None:
    """An in-flight cache read must not expose its result after a confirmed leave."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    mxc_url = "mxc://server/plaintext"
    event = _event(event_id, 1, sidecar_url=mxc_url)
    read_obtained_result = asyncio.Event()
    release_read = asyncio.Event()
    if isinstance(cache, SqliteEventCache):
        sqlite_loader = (
            sqlite_event_cache_events.load_event if lookup_kind == "event" else sqlite_event_cache_events.load_mxc_text
        )
        original_loader = cast("Callable[..., Awaitable[object]]", sqlite_loader)
    else:
        postgres_loader = (
            postgres_event_cache_events.load_event
            if lookup_kind == "event"
            else postgres_event_cache_events.load_mxc_text
        )
        original_loader = cast("Callable[..., Awaitable[object]]", postgres_loader)

    async def pause_after_read(*args: object, **kwargs: object) -> object:
        result = await original_loader(*args, **kwargs)
        read_obtained_result.set()
        await release_read.wait()
        return result

    try:
        await cache.store_event(event_id, room_id, event)
        assert await cache.store_mxc_text(room_id, event_id, mxc_url, "plaintext")
        if isinstance(cache, SqliteEventCache):
            if lookup_kind == "event":
                monkeypatch.setattr(sqlite_event_cache_events, "load_event", pause_after_read)
            else:
                monkeypatch.setattr(sqlite_event_cache_events, "load_mxc_text", pause_after_read)
        elif lookup_kind == "event":
            monkeypatch.setattr(postgres_event_cache_events, "load_event", pause_after_read)
        else:
            monkeypatch.setattr(postgres_event_cache_events, "load_mxc_text", pause_after_read)
        if lookup_kind == "event":
            read_task = asyncio.create_task(cache.get_event(room_id, event_id))
        else:
            read_task = asyncio.create_task(cache.get_mxc_text(room_id, event_id, mxc_url))
        await read_obtained_result.wait()

        cache.mark_room_departed(room_id)
        release_read.set()

        assert await read_task is None
    finally:
        release_read.set()
        await root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_kind", ["event", "mxc"])
@pytest.mark.parametrize("cleanup", ["departure", "departure-rejoin", "principal-purge", "redaction"])
async def test_sqlite_cleanup_in_another_runtime_serializes_with_inflight_read(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lookup_kind: str,
    cleanup: str,
) -> None:
    """Cross-runtime cleanup must commit strictly after an overlapping SQLite read."""
    db_path = tmp_path / "event_cache.db"
    read_root = SqliteEventCache(db_path)
    departure_root = SqliteEventCache(db_path)
    await read_root.initialize()
    await departure_root.initialize()
    principal_id = "@alice:localhost"
    read_cache = read_root.for_principal(principal_id)
    departure_cache = departure_root.for_principal(principal_id)
    room_id = "!left:localhost"
    event_id = "$event"
    mxc_url = "mxc://server/plaintext"
    event = _event(event_id, 1, sidecar_url=mxc_url)
    read_obtained_result = asyncio.Event()
    release_read = asyncio.Event()
    cleanup_write_attempted = asyncio.Event()
    cleanup_write_acquired = asyncio.Event()
    sqlite_loader = (
        sqlite_event_cache_events.load_event if lookup_kind == "event" else sqlite_event_cache_events.load_mxc_text
    )
    original_loader = cast("Callable[..., Awaitable[object]]", sqlite_loader)
    departure_db = departure_root._runtime.require_db()
    original_execute = departure_db.execute

    async def pause_after_read(*args: object, **kwargs: object) -> object:
        result = await original_loader(*args, **kwargs)
        read_obtained_result.set()
        await release_read.wait()
        return result

    async def signal_cleanup_write(sql: str, *args: object, **kwargs: object) -> Cursor:
        if sql != "BEGIN IMMEDIATE":
            return await original_execute(sql, *args, **kwargs)
        cleanup_write_attempted.set()
        cursor = await original_execute(sql, *args, **kwargs)
        cleanup_write_acquired.set()
        return cursor

    try:
        await read_cache.store_event(event_id, room_id, event)
        assert await read_cache.store_mxc_text(room_id, event_id, mxc_url, "plaintext")
        monkeypatch.setattr(departure_db, "execute", signal_cleanup_write)
        if lookup_kind == "event":
            monkeypatch.setattr(sqlite_event_cache_events, "load_event", pause_after_read)
            read_task = asyncio.create_task(read_cache.get_event(room_id, event_id))
        else:
            monkeypatch.setattr(sqlite_event_cache_events, "load_mxc_text", pause_after_read)
            read_task = asyncio.create_task(read_cache.get_mxc_text(room_id, event_id, mxc_url))
        await read_obtained_result.wait()

        async def cleanup_room() -> None:
            if cleanup == "principal-purge":
                await departure_cache.purge_principal()
                return
            if cleanup == "redaction":
                assert await departure_cache.redact_event(room_id, event_id)
                return
            departure_epoch = departure_cache.mark_room_departed(room_id)
            await departure_cache.purge_room(room_id)
            if cleanup == "departure-rejoin":
                await departure_cache.mark_room_joined(
                    room_id,
                    expected_departure_epoch=departure_epoch,
                )

        cleanup_task = asyncio.create_task(cleanup_room())
        await asyncio.wait_for(cleanup_write_attempted.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(cleanup_write_acquired.wait(), timeout=0.25)
        release_read.set()

        assert await read_task == (event if lookup_kind == "event" else "plaintext")
        await cleanup_task
        if lookup_kind == "event":
            assert await read_cache.get_event(room_id, event_id) is None
        else:
            assert await read_cache.get_mxc_text(room_id, event_id, mxc_url) is None
    finally:
        release_read.set()
        await read_root.close()
        await departure_root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purge_scope", ["room", "principal"])
async def test_recovered_purge_discards_the_operation_that_flushes_it(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
    *,
    purge_scope: str,
) -> None:
    """A late write must not recreate rows in the transaction that commits a pending purge."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    old_event = _event("$old", 1)
    late_event = _event("$late", 2)
    await cache.store_event("$old", room_id, old_event)

    if isinstance(cache, SqliteEventCache):
        original_purge = (
            sqlite_event_cache_events.purge_room_locked
            if purge_scope == "room"
            else sqlite_event_cache_events.purge_principal_locked
        )
    else:
        assert isinstance(cache, PostgresEventCache)
        original_purge = (
            postgres_event_cache_events.purge_room_locked
            if purge_scope == "room"
            else postgres_event_cache_events.purge_principal_locked
        )
    failure_reason = "temporary purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    async def purge() -> None:
        if purge_scope == "room":
            await cache.purge_room(room_id)
        else:
            await cache.purge_principal()

    def replace_purge(replacement: Callable[..., Awaitable[None]]) -> None:
        if isinstance(cache, SqliteEventCache):
            if purge_scope == "room":
                monkeypatch.setattr(sqlite_event_cache_events, "purge_room_locked", replacement)
            else:
                monkeypatch.setattr(sqlite_event_cache_events, "purge_principal_locked", replacement)
        elif purge_scope == "room":
            monkeypatch.setattr(postgres_event_cache_events, "purge_room_locked", replacement)
        else:
            monkeypatch.setattr(postgres_event_cache_events, "purge_principal_locked", replacement)

    try:
        replace_purge(fail_purge)
        with pytest.raises(RuntimeError, match=failure_reason):
            await purge()

        replace_purge(original_purge)
        await cache.store_event("$late", room_id, late_event)

        assert await cache.get_event(room_id, "$old") is None
        assert await cache.get_event(room_id, "$late") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_cancelled_prestart_purge_must_flush_before_rejoin(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Cancelling queued cleanup before it starts cannot expose pre-leave rows after rejoin."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$old"
    mxc_url = "mxc://server/old"
    event = _event(event_id, 1, sidecar_url=mxc_url)
    try:
        await cache.store_event(event_id, room_id, event)
        assert await cache.store_mxc_text(room_id, event_id, mxc_url, "old plaintext")

        departure_epoch = cache.mark_room_departed(room_id)
        cancelled_purge = asyncio.create_task(cache.purge_room(room_id))
        cancelled_purge.cancel()
        result = await asyncio.gather(cancelled_purge, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)

        await cache.mark_room_joined(room_id, expected_departure_epoch=departure_epoch)

        assert await cache.get_event(room_id, event_id) is None
        assert await cache.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await _raw_mxc_text_count(cache, room_id, mxc_url) == 0
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_restoring_event_without_thread_relation_removes_stale_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Replacing an event must rebuild rather than accumulate its thread lookup index."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    event_id = "$event"
    threaded_event = _event(event_id, 1)
    threaded_event["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": "$thread-root",
    }
    try:
        await cache.store_event(event_id, room_id, threaded_event)
        assert await cache.get_thread_id_for_event(room_id, event_id) == "$thread-root"
        assert await cache.get_thread_id_for_event(room_id, "$thread-root") == "$thread-root"

        await cache.store_event(event_id, room_id, _event(event_id, 2))

        assert await cache.get_thread_id_for_event(room_id, event_id) is None
        assert await cache.get_thread_id_for_event(room_id, "$thread-root") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_storing_thread_root_preserves_child_proven_self_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A relation-less root event must not erase the self-mapping proven by a surviving child."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    root_event_id = "$thread-root"
    child_event = _event("$child", 1)
    child_event["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    try:
        await cache.store_event("$child", room_id, child_event)
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id

        await cache.store_event(root_event_id, room_id, _event(root_event_id, 2))

        assert await cache.get_thread_id_for_event(room_id, "$child") == root_event_id
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_redacting_last_thread_child_removes_orphan_root_mapping(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The synthetic root self-index must exist exactly while a visible child proves it."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    root_event_id = "$thread-root"
    first_child = _event("$first-child", 1)
    first_child["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    second_child = _event("$second-child", 2)
    second_child["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": root_event_id,
    }
    try:
        await cache.store_events_batch(
            [
                ("$first-child", room_id, first_child),
                ("$second-child", room_id, second_child),
            ],
        )

        assert await cache.redact_event(room_id, "$first-child")
        assert await cache.get_thread_id_for_event(room_id, "$first-child") is None
        assert await cache.get_thread_id_for_event(room_id, root_event_id) == root_event_id

        assert await cache.redact_event(room_id, "$second-child")
        assert await cache.get_thread_id_for_event(room_id, "$second-child") is None
        assert await cache.get_thread_id_for_event(room_id, root_event_id) is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_principal_purge_removes_only_that_principals_rows(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Cold-start cleanup must remove one principal without harming another."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    try:
        await alice.store_event(event_id, room_id, event)
        await bob.store_event(event_id, room_id, event)

        await alice.purge_principal()

        assert await alice.get_event(room_id, event_id) is None
        assert await bob.get_event(room_id, event_id) == event
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_failed_principal_purge_blocks_generation_and_reads_until_recovery(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed cold-start cleanup must remain fail-closed for the current runtime."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!left:localhost"
    event_id = "$event"
    event = _event(event_id, 1)
    await cache.store_event(event_id, room_id, event)

    if isinstance(cache, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(cache, PostgresEventCache)
        module = postgres_event_cache_events
    original_purge = module.purge_principal_locked
    failure_reason = "temporary principal purge failure"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, "purge_principal_locked", fail_purge)
        with pytest.raises(RuntimeError, match="temporary principal purge failure"):
            await cache.purge_principal()
        assert cache.cache_generation is None

        monkeypatch.setattr(module, "purge_principal_locked", original_purge)
        assert await cache.get_event(room_id, event_id) is None
        assert cache.cache_generation is not None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_failed_startup_cleanup_disables_only_the_affected_principal(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed untrusted-row cleanup must keep one principal network-only until restart."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    alice = root.for_principal("@alice:localhost")
    bob = root.for_principal("@bob:localhost")
    room_id = "!room:localhost"
    alice_event = _event("$alice", 1)
    bob_event = _event("$bob", 2)
    await alice.store_event("$alice", room_id, alice_event)
    await bob.store_event("$bob", room_id, bob_event)

    if isinstance(alice, SqliteEventCache):
        module = sqlite_event_cache_events
    else:
        assert isinstance(alice, PostgresEventCache)
        module = postgres_event_cache_events
    failure_reason = "startup principal purge failed"

    async def fail_purge(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(failure_reason)

    try:
        monkeypatch.setattr(module, "purge_principal_locked", fail_purge)
        runtime = MagicMock(event_cache=alice, callback_failure_count=0)
        trust = SyncCacheTrust(
            storage_path=tmp_path,
            agent_name="alice",
            runtime=runtime,
            logger=MagicMock(),
        )
        assert await trust.prepare_startup() is None

        assert alice.durable_writes_available is False
        assert alice.cache_generation is None
        assert await alice.get_event(room_id, "$alice") is None
        await alice.store_event("$late", room_id, _event("$late", 3))
        assert await alice.get_event(room_id, "$late") is None

        assert bob.durable_writes_available is True
        assert await bob.get_event(room_id, "$bob") == bob_event
        await bob.store_event("$new", room_id, _event("$new", 4))
        assert await bob.get_event(room_id, "$new") == _event("$new", 4)
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_redaction_reference_lifecycle_is_durable_and_non_resurrecting(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Redaction preserves shared plaintext, removes the last reference, and tombstones replays."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    principal_id = "@alice:localhost"
    cache = root.for_principal(principal_id)
    room_id = "!room:localhost"
    shared_mxc = "mxc://server/shared"
    dependent_mxc = "mxc://server/dependent"
    top_level = _event("$top", 1, sidecar_url=shared_mxc)
    new_content = _event(
        "$new-content",
        2,
        sidecar_url=shared_mxc,
        encrypted=True,
        sidecar_in_new_content=True,
    )
    original = _event("$original", 3, sidecar_url=dependent_mxc)
    edit = _event(
        "$edit",
        4,
        sidecar_url=dependent_mxc,
        encrypted=True,
        sidecar_in_new_content=True,
        edit_of="$original",
    )
    try:
        await cache.store_events_batch(
            [
                ("$top", room_id, top_level),
                ("$new-content", room_id, new_content),
                ("$original", room_id, original),
                ("$edit", room_id, edit),
            ],
        )
        assert await cache.store_mxc_text(room_id, "$top", shared_mxc, "shared plaintext")
        assert await cache.store_mxc_text(room_id, "$original", dependent_mxc, "dependent plaintext")
        assert await _raw_mxc_text_count(cache, room_id, shared_mxc) == 1
        assert await _raw_mxc_text_count(cache, room_id, dependent_mxc) == 1

        assert await cache.redact_event(room_id, "$top")
        assert await cache.get_mxc_text(room_id, "$top", shared_mxc) is None
        assert await cache.get_mxc_text(room_id, "$new-content", shared_mxc) == "shared plaintext"
        assert await _raw_mxc_text_count(cache, room_id, shared_mxc) == 1

        assert await cache.redact_event(room_id, "$new-content")
        assert await cache.get_mxc_text(room_id, "$new-content", shared_mxc) is None
        assert await _raw_mxc_text_count(cache, room_id, shared_mxc) == 0

        assert await cache.redact_event(room_id, "$original")
        assert await cache.get_event(room_id, "$original") is None
        assert await cache.get_event(room_id, "$edit") is None
        assert await cache.get_mxc_text(room_id, "$edit", dependent_mxc) is None
        assert await _raw_mxc_text_count(cache, room_id, dependent_mxc) == 0
    finally:
        await root.close()

    await _assert_redacted_events_do_not_resurrect(
        event_cache_factory,
        principal_id=principal_id,
        room_id=room_id,
        shared_mxc=shared_mxc,
        dependent_mxc=dependent_mxc,
        events=[
            ("$top", room_id, top_level),
            ("$new-content", room_id, new_content),
            ("$original", room_id, original),
            ("$edit", room_id, edit),
        ],
    )


@pytest.mark.asyncio
async def test_thread_refresh_prunes_only_plaintext_absent_from_replacement(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A replacement installs surviving references before pruning removed plaintext."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    thread_id = "$thread"
    surviving_mxc = "mxc://server/surviving"
    removed_mxc = "mxc://server/removed"
    surviving_event = _event("$surviving", 1, sidecar_url=surviving_mxc)
    removed_event = _event("$removed", 2, sidecar_url=removed_mxc, encrypted=True)
    try:
        await replace_thread_unconditionally(
            cache,
            room_id,
            thread_id,
            [surviving_event, removed_event],
            validated_at=1.0,
        )
        assert await cache.store_mxc_text(room_id, "$surviving", surviving_mxc, "surviving plaintext")
        assert await cache.store_mxc_text(room_id, "$removed", removed_mxc, "removed plaintext")

        await replace_thread_unconditionally(
            cache,
            room_id,
            thread_id,
            [surviving_event],
            validated_at=2.0,
        )

        assert await cache.get_mxc_text(room_id, "$surviving", surviving_mxc) == "surviving plaintext"
        assert await _raw_mxc_text_count(cache, room_id, surviving_mxc) == 1
        assert await cache.get_mxc_text(room_id, "$removed", removed_mxc) is None
        assert await _raw_mxc_text_count(cache, room_id, removed_mxc) == 0
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_pre_departure_thread_refill_cannot_resurrect_after_rejoin(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A fetch from an earlier membership epoch cannot replace a purged room snapshot."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    cache = root.for_principal("@alice:localhost")
    room_id = "!room:localhost"
    thread_id = "$thread"
    root_event = _event(thread_id, 1, body="root")
    redacted_event = _event("$redacted", 2, body="secret")
    try:
        await replace_thread_unconditionally(
            cache,
            room_id,
            thread_id,
            [root_event, redacted_event],
            validated_at=50.0,
        )
        fetch_membership_epoch = await cache.room_membership_epoch(room_id)
        assert await cache.redact_event(room_id, "$redacted")

        departure_epoch = cache.mark_room_departed(room_id)
        await cache.purge_room(room_id)
        await cache.mark_room_joined(room_id, expected_departure_epoch=departure_epoch)

        replaced = await cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            [root_event, redacted_event],
            expected_membership_epoch=fetch_membership_epoch,
            fetch_started_at=100.0,
        )

        assert replaced is False
        assert await cache.get_thread_events(room_id, thread_id) is None
        assert await cache.get_event(room_id, "$redacted") is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_pre_departure_thread_refill_from_another_runtime_cannot_resurrect(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A durable departure marker must reject stale work from another cache runtime."""
    departing_root = _shared_cache(event_cache_factory)
    stale_root = _shared_cache(event_cache_factory)
    await departing_root.initialize()
    await stale_root.initialize()
    principal_id = "@alice:localhost"
    departing_cache = departing_root.for_principal(principal_id)
    stale_cache = stale_root.for_principal(principal_id)
    room_id = "!room:localhost"
    thread_id = "$thread"
    events = [_event(thread_id, 1, body="root"), _event("$secret", 2, body="secret")]
    try:
        await replace_thread_unconditionally(
            departing_cache,
            room_id,
            thread_id,
            events,
            validated_at=50.0,
        )
        stale_membership_epoch = await stale_cache.room_membership_epoch(room_id)

        departure_epoch = departing_cache.mark_room_departed(room_id)
        await departing_cache.purge_room(room_id)
        await departing_cache.mark_room_joined(
            room_id,
            expected_departure_epoch=departure_epoch,
        )

        state = await departing_cache.get_thread_cache_state(room_id, thread_id)
        assert state is not None
        assert state.room_invalidated_at is not None
        assert state.room_invalidation_reason == "room_rejoined"

        replaced = await stale_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            events,
            expected_membership_epoch=stale_membership_epoch,
            fetch_started_at=state.room_invalidated_at - 1.0,
        )

        assert replaced is False
        assert await departing_cache.get_thread_events(room_id, thread_id) is None
        assert await departing_cache.get_event(room_id, "$secret") is None

        assert await stale_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            events,
            expected_membership_epoch=await stale_cache.room_membership_epoch(room_id),
            fetch_started_at=state.room_invalidated_at + 1.0,
        )
    finally:
        await stale_root.close()
        await departing_root.close()


@pytest.mark.asyncio
async def test_departed_refill_guard_blocks_point_plaintext_and_thread_writes_after_rejoin(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A refill begun while departed cannot write through another runtime after rejoin."""
    membership_root = _shared_cache(event_cache_factory)
    refill_root = _shared_cache(event_cache_factory)
    await membership_root.initialize()
    await refill_root.initialize()
    principal_id = "@alice:localhost"
    membership_cache = membership_root.for_principal(principal_id)
    refill_cache = refill_root.for_principal(principal_id)
    room_id = "!room:localhost"
    thread_id = "$thread"
    event_id = "$sidecar"
    mxc_url = "mxc://server/departed-refill"
    events = [
        _event(thread_id, 1, body="root"),
        _event(event_id, 2, sidecar_url=mxc_url, encrypted=True),
    ]
    try:
        departure_epoch = membership_cache.mark_room_departed(room_id)
        await membership_cache.purge_room(room_id)
        departed_membership_epoch = await refill_cache.room_membership_epoch(room_id)

        await refill_cache.store_event(event_id, room_id, events[1])
        assert await refill_cache.get_event(room_id, event_id) is None
        assert not await refill_cache.store_mxc_text(
            room_id,
            event_id,
            mxc_url,
            "departed plaintext",
            expected_membership_epoch=departed_membership_epoch,
        )

        await membership_cache.mark_room_joined(
            room_id,
            expected_departure_epoch=departure_epoch,
        )
        joined_membership_epoch = await refill_cache.room_membership_epoch(room_id)
        assert joined_membership_epoch > departed_membership_epoch

        await refill_cache.invalidate_room_threads(room_id)
        assert await refill_cache.room_membership_epoch(room_id) == joined_membership_epoch

        await refill_cache.store_event(
            event_id,
            room_id,
            events[1],
            expected_membership_epoch=departed_membership_epoch,
        )
        assert await refill_cache.get_event(room_id, event_id) is None
        assert not await refill_cache.store_mxc_text(
            room_id,
            event_id,
            mxc_url,
            "stale plaintext",
            expected_membership_epoch=departed_membership_epoch,
        )
        assert not await refill_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            events,
            expected_membership_epoch=departed_membership_epoch,
            fetch_started_at=float("inf"),
        )
        assert await _raw_mxc_text_count(refill_cache, room_id, mxc_url) == 0

        assert await refill_cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            events,
            expected_membership_epoch=joined_membership_epoch,
            fetch_started_at=float("inf"),
        )
        assert await refill_cache.store_mxc_text(
            room_id,
            event_id,
            mxc_url,
            "fresh plaintext",
            expected_membership_epoch=joined_membership_epoch,
        )
    finally:
        await refill_root.close()
        await membership_root.close()


@pytest.mark.asyncio
async def test_durable_departure_can_rejoin_after_cache_runtime_restart(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """An authoritative join must recover a durable departed row after process restart."""
    first_root = _shared_cache(event_cache_factory)
    await first_root.initialize()
    principal_id = "@alice:localhost"
    room_id = "!room:localhost"
    first = first_root.for_principal(principal_id)
    first.mark_room_departed(room_id)
    await first.purge_room(room_id)
    departed_membership_epoch = await first.room_membership_epoch(room_id)
    await first_root.close()

    second_root = _shared_cache(event_cache_factory)
    await second_root.initialize()
    second = second_root.for_principal(principal_id)
    try:
        await second.mark_room_joined(
            room_id,
            expected_departure_epoch=second.room_departure_epoch(room_id),
        )
        joined_membership_epoch = await second.room_membership_epoch(room_id)
        assert departed_membership_epoch is not None
        assert joined_membership_epoch is not None
        assert joined_membership_epoch > departed_membership_epoch

        event = _event("$joined", 1)
        await second.store_event("$joined", room_id, event)
        assert await second.get_event(room_id, "$joined") == event
    finally:
        await second_root.close()


@pytest.mark.asyncio
async def test_newer_departure_during_rejoin_closes_durable_room(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer departure observed inside a rejoin transaction purges before commit."""
    root = _shared_cache(event_cache_factory)
    observer_root = _shared_cache(event_cache_factory)
    await root.initialize()
    await observer_root.initialize()
    principal_id = "@alice:localhost"
    room_id = "!room:localhost"
    event_id = "$pre-departure"
    cache = root.for_principal(principal_id)
    observer = observer_root.for_principal(principal_id)
    await cache.store_event(event_id, room_id, _event(event_id, 1))
    expected_departure_epoch = cache.room_departure_epoch(room_id)
    load_started = asyncio.Event()
    release_load = asyncio.Event()
    if isinstance(cache, SqliteEventCache):
        original_load = sqlite_event_cache_threads.load_room_membership_locked
        module = sqlite_event_cache_threads
    else:
        assert isinstance(cache, PostgresEventCache)
        original_load = postgres_event_cache_threads.load_room_membership_locked
        module = postgres_event_cache_threads

    async def pause_membership_load(*args: object, **kwargs: object) -> tuple[str, int]:
        result = await original_load(*args, **kwargs)
        load_started.set()
        await release_load.wait()
        return result

    monkeypatch.setattr(module, "load_room_membership_locked", pause_membership_load)
    join_task = asyncio.create_task(
        cache.mark_room_joined(
            room_id,
            expected_departure_epoch=expected_departure_epoch,
        ),
    )
    try:
        await load_started.wait()
        cache.mark_room_departed(room_id)
        release_load.set()
        await join_task

        assert await observer.get_event(room_id, event_id) is None
        await observer.store_event("$stale", room_id, _event("$stale", 2))
        assert await observer.get_event(room_id, "$stale") is None
    finally:
        release_load.set()
        if not join_task.done():
            await join_task
        await observer_root.close()
        await root.close()


@pytest.mark.asyncio
async def test_principal_purge_advances_certified_room_refill_epoch(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A cold-start principal purge rejects source writes certified before cleanup."""
    purge_root = _shared_cache(event_cache_factory)
    stale_root = _shared_cache(event_cache_factory)
    await purge_root.initialize()
    await stale_root.initialize()
    principal_id = "@alice:localhost"
    room_id = "!room:localhost"
    purge_cache = purge_root.for_principal(principal_id)
    stale_cache = stale_root.for_principal(principal_id)
    stale_epoch = await stale_cache.room_membership_epoch(room_id)
    assert stale_epoch is not None
    try:
        await purge_cache.purge_principal()
        current_epoch = await purge_cache.room_membership_epoch(room_id)
        assert current_epoch is not None
        assert current_epoch > stale_epoch

        await stale_cache.store_event(
            "$stale",
            room_id,
            _event("$stale", 1),
            expected_membership_epoch=stale_epoch,
        )
        assert await stale_cache.get_event(room_id, "$stale") is None

        event = _event("$fresh", 2)
        await stale_cache.store_event(
            "$fresh",
            room_id,
            event,
            expected_membership_epoch=current_epoch,
        )
        assert await stale_cache.get_event(room_id, "$fresh") == event
    finally:
        await stale_root.close()
        await purge_root.close()


@pytest.mark.asyncio
async def test_cached_sidecar_hydration_cannot_cross_principal_purge(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held cached snapshot must keep its pre-purge epoch through hydration."""
    purge_root = _shared_cache(event_cache_factory)
    reader_root = _shared_cache(event_cache_factory)
    await purge_root.initialize()
    await reader_root.initialize()
    principal_id = "@alice:localhost"
    room_id = "!room:localhost"
    thread_id = "$thread"
    sidecar_event_id = "$sidecar"
    mxc_url = "mxc://server/held-before-purge"
    purge_cache = purge_root.for_principal(principal_id)
    reader_cache = reader_root.for_principal(principal_id)
    sidecar_event = _event(sidecar_event_id, 2, sidecar_url=mxc_url)
    sidecar_event["content"]["m.relates_to"] = {
        "rel_type": "m.thread",
        "event_id": thread_id,
    }
    await replace_thread_unconditionally(
        reader_cache,
        room_id,
        thread_id,
        [_event(thread_id, 1, body="root"), sidecar_event],
    )
    rows_loaded = asyncio.Event()
    release_rows = asyncio.Event()
    original_get_thread_events = reader_cache.get_thread_events

    async def pause_after_read(read_room_id: str, read_thread_id: str) -> list[dict[str, Any]] | None:
        rows = await original_get_thread_events(read_room_id, read_thread_id)
        rows_loaded.set()
        await release_rows.wait()
        return rows

    monkeypatch.setattr(reader_cache, "get_thread_events", pause_after_read)
    download_response = MagicMock(spec=nio.DownloadResponse)
    download_response.body = b'{"msgtype":"m.text","body":"secret plaintext"}'
    client = MagicMock(spec=nio.AsyncClient)
    client.download = AsyncMock(return_value=download_response)
    read_task = asyncio.create_task(
        client_thread_history._load_cached_thread_history_if_usable(
            client,
            room_id=room_id,
            thread_id=thread_id,
            event_cache=reader_cache,
            hydrate_sidecars=True,
        ),
    )
    try:
        await rows_loaded.wait()
        await purge_cache.purge_principal()
        release_rows.set()
        cached_history, _diagnostics = await read_task

        assert cached_history is not None
        assert await reader_cache.get_event(room_id, sidecar_event_id) is None
        assert await _raw_mxc_text_count(reader_cache, room_id, mxc_url) == 0
    finally:
        release_rows.set()
        if not read_task.done():
            await read_task
        await reader_root.close()
        await purge_root.close()


@pytest.mark.asyncio
async def test_cached_sidecar_hydration_after_restart_preserves_event_age(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Hydrating persisted plaintext must not make an old room-level event look new."""
    principal_id = "@agent:localhost"
    room_id = "!room:localhost"
    event_id = "$sidecar"
    mxc_url = "mxc://server/persisted-sidecar"
    event = _event(event_id, 1, sidecar_url=mxc_url)
    initial_root = _shared_cache(event_cache_factory)
    await initial_root.initialize()
    initial_cache = initial_root.for_principal(principal_id)
    await initial_cache.store_event(event_id, room_id, event)
    assert await initial_cache.store_mxc_text(room_id, event_id, mxc_url, '{"body":"persisted"}')
    await initial_root.close()

    restarted_root = _shared_cache(event_cache_factory)
    await restarted_root.initialize()
    restarted_cache = restarted_root.for_principal(principal_id)
    runtime_started_at = time.time()
    membership_epoch = await restarted_cache.room_membership_epoch(room_id)
    assert membership_epoch is not None
    client = MagicMock(spec=nio.AsyncClient)
    client.download = AsyncMock(side_effect=AssertionError("persisted plaintext should be reused"))
    try:
        resolved_event = await resolve_event_source_content(
            event,
            client,
            event_cache=restarted_cache,
            room_id=room_id,
            expected_membership_epoch=membership_epoch,
        )
        snapshot = await restarted_cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id=None,
            sender=principal_id,
            runtime_started_at=runtime_started_at,
        )
    finally:
        await restarted_root.close()

    assert resolved_event["content"]["body"] == "persisted"
    assert snapshot is None
    client.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_leave_purges_each_room_before_processing_the_next(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during a later room cannot skip an earlier confirmed departure."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    principal_id = "@alice:localhost"
    cache = root.for_principal(principal_id)
    departed_room_id = "!departed:localhost"
    pending_room_id = "!pending:localhost"
    event_id = "$sidecar"
    mxc_url = "mxc://server/departed"
    event = _event(event_id, 1, sidecar_url=mxc_url, encrypted=True)
    second_room_waiting = asyncio.Event()
    release_second_room = asyncio.Event()

    async def is_dm_room(_client: object, room_id: str) -> bool:
        if room_id == pending_room_id:
            second_room_waiting.set()
            await release_second_room.wait()
        return False

    async def leave_room(_client: object, room_id: str) -> bool:
        assert room_id == departed_room_id
        return True

    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", is_dm_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    await cache.store_event(event_id, departed_room_id, event)
    assert await cache.store_mxc_text(departed_room_id, event_id, mxc_url, "departed plaintext")

    leave_task = asyncio.create_task(
        leave_non_dm_rooms(
            cast("Any", object()),
            [departed_room_id, pending_room_id],
            on_room_left=cache.purge_room,
        ),
    )
    try:
        await second_room_waiting.wait()
        assert cache.room_departure_epoch(departed_room_id) > 0
        assert await cache.get_event(departed_room_id, event_id) is None
        assert await cache.get_mxc_text(departed_room_id, event_id, mxc_url) is None
        assert await _raw_mxc_text_count(cache, departed_room_id, mxc_url) == 0

        leave_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leave_task
    finally:
        if not leave_task.done():
            leave_task.cancel()
            await asyncio.gather(leave_task, return_exceptions=True)
        await root.close()

    await _assert_room_purge_survives_restart(
        event_cache_factory,
        principal_id=principal_id,
        room_id=departed_room_id,
        event_id=event_id,
        mxc_url=mxc_url,
    )


@pytest.mark.asyncio
async def test_proactive_leave_cancellation_after_server_commit_finishes_cleanup(
    event_cache_factory: Callable[[], ConversationEventCache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot abort cleanup after the homeserver commits a leave."""
    root = _shared_cache(event_cache_factory)
    await root.initialize()
    principal_id = "@alice:localhost"
    cache = root.for_principal(principal_id)
    room_id = "!departed:localhost"
    event_id = "$sidecar"
    mxc_url = "mxc://server/departed"
    event = _event(event_id, 1, sidecar_url=mxc_url, encrypted=True)
    server_committed = asyncio.Event()
    deliver_response = asyncio.Event()
    leave_request_cancelled = False

    async def is_dm_room(_client: object, _room_id: str) -> bool:
        return False

    async def leave_room(_client: object, _room_id: str) -> bool:
        nonlocal leave_request_cancelled
        server_committed.set()
        try:
            await deliver_response.wait()
        except asyncio.CancelledError:
            leave_request_cancelled = True
            raise
        return True

    monkeypatch.setattr("mindroom.matrix.rooms.is_dm_room", is_dm_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    await cache.store_event(event_id, room_id, event)
    assert await cache.store_mxc_text(room_id, event_id, mxc_url, "departed plaintext")

    leave_task = asyncio.create_task(
        leave_non_dm_rooms(
            cast("Any", object()),
            [room_id],
            on_room_left=cache.purge_room,
        ),
    )
    try:
        await server_committed.wait()
        leave_task.cancel()
        deliver_response.set()
        with pytest.raises(asyncio.CancelledError):
            await leave_task

        assert leave_request_cancelled is False
        assert cache.room_departure_epoch(room_id) > 0
        assert await cache.get_event(room_id, event_id) is None
        assert await cache.get_mxc_text(room_id, event_id, mxc_url) is None
        assert await _raw_mxc_text_count(cache, room_id, mxc_url) == 0
    finally:
        deliver_response.set()
        if not leave_task.done():
            leave_task.cancel()
            await asyncio.gather(leave_task, return_exceptions=True)
        await root.close()

    await _assert_room_purge_survives_restart(
        event_cache_factory,
        principal_id=principal_id,
        room_id=room_id,
        event_id=event_id,
        mxc_url=mxc_url,
    )


@pytest.mark.asyncio
async def test_postgres_principal_purge_excludes_other_runtime_operations(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace purge transaction must exclude room operations from another runtime."""
    namespace = f"purge_lock_{uuid.uuid4().hex}"
    first = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    second = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    room_id = "!room:localhost"
    event_id = "$after-purge"
    event = _event(event_id, 1)
    purge_deleted = asyncio.Event()
    release_purge = asyncio.Event()
    store_lock_attempted = asyncio.Event()
    original_purge = postgres_event_cache_events.purge_principal_locked
    original_acquire = second._runtime.acquire_db_operation

    async def pause_after_delete(*args: object, **kwargs: object) -> None:
        await original_purge(*args, **kwargs)
        purge_deleted.set()
        await release_purge.wait()

    @asynccontextmanager
    async def signal_store_lock_attempt(
        *,
        operation: str,
    ) -> AsyncIterator[Any]:
        store_lock_attempted.set()
        async with original_acquire(operation=operation) as db:
            yield db

    await first.initialize()
    await second.initialize()
    monkeypatch.setattr(postgres_event_cache_events, "purge_principal_locked", pause_after_delete)
    monkeypatch.setattr(second._runtime, "acquire_db_operation", signal_store_lock_attempt)
    purge_task = asyncio.create_task(first.purge_principal())
    store_task: asyncio.Task[None] | None = None
    try:
        await purge_deleted.wait()
        store_task = asyncio.create_task(second.store_event(event_id, room_id, event))
        await store_lock_attempted.wait()

        assert not store_task.done()

        release_purge.set()
        await purge_task
        await store_task
        assert await second.get_event(room_id, event_id) == event
    finally:
        release_purge.set()
        if not purge_task.done():
            await purge_task
        if store_task is not None and not store_task.done():
            await store_task
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_postgres_resumed_principal_purge_uses_namespace_lock(
    postgres_event_cache_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary operation resuming a failed purge must still exclude every room writer."""
    namespace = f"resumed_purge_lock_{uuid.uuid4().hex}"
    first = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    second = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    first_room_id = "!first:localhost"
    second_room_id = "!second:localhost"
    event_id = "$after-purge"
    event = _event(event_id, 1)
    purge_deleted = asyncio.Event()
    release_purge = asyncio.Event()
    store_lock_attempted = asyncio.Event()
    original_purge = postgres_event_cache_events.purge_principal_locked
    original_acquire = second._runtime.acquire_db_operation
    fail_initial_purge = True
    failure_reason = "temporary purge failure"

    async def control_purge(*args: object, **kwargs: object) -> None:
        if fail_initial_purge:
            raise RuntimeError(failure_reason)
        await original_purge(*args, **kwargs)
        purge_deleted.set()
        await release_purge.wait()

    @asynccontextmanager
    async def signal_store_lock_attempt(
        *,
        operation: str,
    ) -> AsyncIterator[Any]:
        store_lock_attempted.set()
        async with original_acquire(operation=operation) as db:
            yield db

    await first.initialize()
    await second.initialize()
    monkeypatch.setattr(postgres_event_cache_events, "purge_principal_locked", control_purge)
    monkeypatch.setattr(second._runtime, "acquire_db_operation", signal_store_lock_attempt)
    try:
        with pytest.raises(RuntimeError, match="temporary purge failure"):
            await first.purge_principal()
        fail_initial_purge = False

        resumed_purge = asyncio.create_task(first.get_event(first_room_id, "$missing"))
        await purge_deleted.wait()
        store_task = asyncio.create_task(second.store_event(event_id, second_room_id, event))
        await store_lock_attempted.wait()

        assert not store_task.done()

        release_purge.set()
        assert await resumed_purge is None
        await store_task
        assert await second.get_event(second_room_id, event_id) == event
    finally:
        release_purge.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sqlite_write_transaction_serializes_tombstone_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite must lock before read-based event tombstone authorization."""
    db_path = tmp_path / "event-cache.db"
    principal_id = "@alice:localhost"
    first = SqliteEventCache(db_path, principal_id=principal_id)
    second = SqliteEventCache(db_path, principal_id=principal_id)
    room_id = "!room:localhost"
    event_id = "$late-event"
    event = _event(event_id, 1)
    event_checked = asyncio.Event()
    release_event_write = asyncio.Event()
    redaction_write_attempted = asyncio.Event()
    original_filter = sqlite_event_cache_events.filter_cacheable_events

    async def pause_after_tombstone_check(*args: object, **kwargs: object) -> object:
        cacheable = await original_filter(*args, **kwargs)
        event_checked.set()
        await release_event_write.wait()
        return cacheable

    await first.initialize()
    await second.initialize()
    second_db = second._runtime.require_db()
    original_execute = second_db.execute

    async def signal_redaction_write(sql: str, *args: object, **kwargs: object) -> Cursor:
        if sql == "BEGIN IMMEDIATE":
            redaction_write_attempted.set()
        return await original_execute(sql, *args, **kwargs)

    try:
        monkeypatch.setattr(sqlite_event_cache_events, "filter_cacheable_events", pause_after_tombstone_check)
        monkeypatch.setattr(second_db, "execute", signal_redaction_write)
        late_store = asyncio.create_task(first.store_event(event_id, room_id, event))
        await event_checked.wait()
        redact_late_event = asyncio.create_task(second.redact_event(room_id, event_id))
        await redaction_write_attempted.wait()
        assert not redact_late_event.done()

        release_event_write.set()
        await late_store
        assert await redact_late_event
        assert await first.get_event(room_id, event_id) is None
    finally:
        release_event_write.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sqlite_write_transaction_serializes_mxc_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite must lock before read-based plaintext ownership authorization."""
    db_path = tmp_path / "event-cache.db"
    principal_id = "@alice:localhost"
    first = SqliteEventCache(db_path, principal_id=principal_id)
    second = SqliteEventCache(db_path, principal_id=principal_id)
    room_id = "!room:localhost"
    sidecar_event_id = "$sidecar"
    mxc_url = "mxc://server/plaintext"
    ownership_checked = asyncio.Event()
    release_plaintext_write = asyncio.Event()
    redaction_write_attempted = asyncio.Event()
    original_ownership_check = sqlite_event_cache_events._event_owns_mxc_text

    async def pause_after_ownership_check(*args: object, **kwargs: object) -> object:
        owns_plaintext = await original_ownership_check(*args, **kwargs)
        ownership_checked.set()
        await release_plaintext_write.wait()
        return owns_plaintext

    await first.initialize()
    await second.initialize()
    second_db = second._runtime.require_db()
    original_execute = second_db.execute

    async def signal_redaction_write(sql: str, *args: object, **kwargs: object) -> Cursor:
        if sql == "BEGIN IMMEDIATE":
            redaction_write_attempted.set()
        return await original_execute(sql, *args, **kwargs)

    try:
        await first.store_event(
            sidecar_event_id,
            room_id,
            _event(sidecar_event_id, 2, sidecar_url=mxc_url),
        )
        monkeypatch.setattr(sqlite_event_cache_events, "_event_owns_mxc_text", pause_after_ownership_check)
        monkeypatch.setattr(second_db, "execute", signal_redaction_write)
        plaintext_store = asyncio.create_task(
            first.store_mxc_text(room_id, sidecar_event_id, mxc_url, "plaintext"),
        )
        await ownership_checked.wait()
        redact_sidecar = asyncio.create_task(second.redact_event(room_id, sidecar_event_id))
        await redaction_write_attempted.wait()
        assert not redact_sidecar.done()

        release_plaintext_write.set()
        assert await plaintext_store
        assert await redact_sidecar
        assert await first.get_mxc_text(room_id, sidecar_event_id, mxc_url) is None
        cursor = await first._runtime.require_db().execute(
            """
            SELECT COUNT(*)
            FROM mxc_text_cache
            WHERE principal_id = ? AND room_id = ? AND mxc_url = ?
            """,
            (principal_id, room_id, mxc_url),
        )
        assert await cursor.fetchone() == (0,)
        await cursor.close()
    finally:
        release_plaintext_write.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_sqlite_plaintext_write_result_is_rejected_after_departure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leave observed during plaintext authorization must reject the committed write result."""
    cache = SqliteEventCache(tmp_path / "event-cache.db", principal_id="@alice:localhost")
    room_id = "!room:localhost"
    event_id = "$sidecar"
    mxc_url = "mxc://server/plaintext"
    ownership_checked = asyncio.Event()
    release_plaintext_write = asyncio.Event()
    original_ownership_check = sqlite_event_cache_events._event_owns_mxc_text

    async def pause_after_ownership_check(*args: object, **kwargs: object) -> object:
        owns_plaintext = await original_ownership_check(*args, **kwargs)
        ownership_checked.set()
        await release_plaintext_write.wait()
        return owns_plaintext

    await cache.initialize()
    try:
        await cache.store_event(event_id, room_id, _event(event_id, 1, sidecar_url=mxc_url))
        monkeypatch.setattr(sqlite_event_cache_events, "_event_owns_mxc_text", pause_after_ownership_check)
        plaintext_store = asyncio.create_task(
            cache.store_mxc_text(room_id, event_id, mxc_url, "plaintext"),
        )
        await ownership_checked.wait()

        departure_epoch = cache.mark_room_departed(room_id)
        release_plaintext_write.set()

        assert await plaintext_store is False
        assert await cache.get_mxc_text(room_id, event_id, mxc_url) is None
        await cache.mark_room_joined(room_id, expected_departure_epoch=departure_epoch)
        assert await _raw_mxc_text_count(cache, room_id, mxc_url) == 0
    finally:
        release_plaintext_write.set()
        await cache.close()
