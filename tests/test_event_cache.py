"""Tests for the SQLite-backed Matrix thread event cache."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio.api import RelationshipType

import mindroom.matrix.cache.sqlite_event_cache as event_cache_module
from mindroom.matrix.cache import event_normalization, sqlite_event_cache_events, sqlite_event_cache_threads
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.client_thread_history import fetch_thread_history
from mindroom.matrix.conversation_cache import _cached_room_get_event as cached_room_get_event
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import _clear_mxc_cache
from mindroom.timing import DispatchPipelineTiming

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def test_sqlite_event_cache_is_explicit_concrete_cache(tmp_path: Path) -> None:
    """The SQLite cache implementation should be named at the boundary."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")

    assert cache.db_path == tmp_path / "event_cache.db"


def _make_text_event(
    *,
    event_id: str,
    sender: str,
    body: str,
    server_timestamp: int,
    source_content: dict[str, object],
) -> MagicMock:
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = sender
    event.body = body
    event.server_timestamp = server_timestamp
    normalized_content = dict(source_content)
    normalized_content.setdefault("msgtype", "m.text")
    event.source = {
        "type": "m.room.message",
        "content": normalized_content,
    }
    return event


def _cache_source(event: nio.Event) -> dict[str, object]:
    source = dict(event.source)
    content = dict(source.get("content", {}))
    content.setdefault("msgtype", "m.text")
    source["content"] = content
    source.setdefault("event_id", event.event_id)
    source.setdefault("sender", event.sender)
    source.setdefault("origin_server_ts", event.server_timestamp)
    return source


def _make_room_get_event_response(event: nio.Event) -> MagicMock:
    response = MagicMock(spec=nio.RoomGetEventResponse)
    response.event = event
    return response


def _relation_key(
    event_id: str,
    rel_type: RelationshipType,
    *,
    event_type: str = "m.room.message",
    direction: nio.MessageDirection = nio.MessageDirection.back,
    limit: int | None = None,
) -> tuple[str, RelationshipType, str, nio.MessageDirection, int | None]:
    return (event_id, rel_type, event_type, direction, limit)


def _make_relations_client(
    *,
    root_event: nio.Event,
    relations: dict[
        tuple[str, RelationshipType, str, nio.MessageDirection, int | None],
        Iterable[nio.Event] | Exception,
    ],
) -> MagicMock:
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(root_event))

    def room_get_event_relations(
        _room_id: str,
        event_id: str,
        *,
        rel_type: RelationshipType | None = None,
        event_type: str | None = None,
        direction: nio.MessageDirection = nio.MessageDirection.back,
        limit: int | None = None,
    ) -> object:
        assert rel_type is not None
        assert event_type is not None
        value = relations.get((event_id, rel_type, event_type, direction, limit), [])

        async def iterator() -> object:
            if isinstance(value, Exception):
                raise value
            for event in value:
                yield event

        return iterator()

    client.room_get_event_relations = MagicMock(side_effect=room_get_event_relations)
    room_scan_chunk: list[nio.Event] = [root_event]
    seen_event_ids = {getattr(root_event, "event_id", None)}
    for value in relations.values():
        if isinstance(value, Exception):
            continue
        for event in value:
            event_id = getattr(event, "event_id", None)
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            room_scan_chunk.insert(-1, event)
    client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!room:localhost",
            chunk=room_scan_chunk,
            start="",
            end=None,
        ),
    )
    return client


async def _seed_thread_cache(
    cache: SqliteEventCache,
    *,
    room_id: str,
    thread_id: str,
    events: list[dict[str, object]],
) -> None:
    """Seed one authoritative cached thread snapshot for tests."""
    await cache.replace_thread(room_id, thread_id, events)


def test_event_cache_normalization_is_backend_neutral() -> None:
    """Cache payload normalization should stay backend-neutral."""
    normalized_event = event_normalization.normalize_event_source_for_cache(
        {
            "type": "m.room.message",
            "content": {"body": "hello"},
            "com.mindroom.dispatch_pipeline_timing": {"resolution_ms": 12},
        },
        event_id="$event",
        sender="@user:localhost",
        origin_server_ts=1234,
    )

    assert normalized_event == {
        "type": "m.room.message",
        "content": {"body": "hello"},
        "event_id": "$event",
        "sender": "@user:localhost",
        "origin_server_ts": 1234,
    }


@pytest.mark.asyncio
async def test_thread_snapshot_storage_exposes_direct_cache_state_reads(tmp_path: Path) -> None:
    """Thread snapshot ownership should expose joined thread and room cache state."""
    db = await event_cache_module.initialize_event_cache_db(tmp_path / "event_cache.db")

    try:
        await sqlite_event_cache_threads.replace_thread_locked(
            db,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[
                {
                    "event_id": "$thread_root",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"body": "Root message", "msgtype": "m.text"},
                },
            ],
            validated_at=100.0,
        )
        await sqlite_event_cache_threads.mark_thread_stale_locked(
            db,
            room_id="!room:localhost",
            thread_id="$thread_root",
            reason="thread_stale",
        )
        await sqlite_event_cache_threads.mark_room_stale_locked(
            db,
            room_id="!room:localhost",
            reason="room_stale",
        )
        await db.commit()

        state = await sqlite_event_cache_threads.load_thread_cache_state(
            db,
            room_id="!room:localhost",
            thread_id="$thread_root",
        )
    finally:
        await db.close()

    assert state is not None
    assert state.validated_at == 100.0
    assert state.invalidation_reason == "thread_stale"
    assert state.room_invalidation_reason == "room_stale"


@pytest.mark.asyncio
async def test_event_cache_store_and_retrieve(tmp_path: Path) -> None:
    """Stored events should round-trip in timestamp order."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[
                {
                    "event_id": "$reply",
                    "sender": "@agent:localhost",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "body": "Reply in thread",
                        "msgtype": "m.text",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                    },
                },
                {
                    "event_id": "$thread_root",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"body": "Root message", "msgtype": "m.text"},
                },
            ],
        )

        cached_events = await cache.get_thread_events("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert cached_events is not None
    assert [event["event_id"] for event in cached_events] == ["$thread_root", "$reply"]


@pytest.mark.asyncio
async def test_get_recent_room_thread_ids_orders_by_latest_event_in_each_thread(tmp_path: Path) -> None:
    """Recent thread IDs should be ordered by the freshest cached event per thread, not by root timestamp."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_old_root_recent_reply",
            events=[
                {
                    "event_id": "$thread_old_root_recent_reply",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"body": "Old root", "msgtype": "m.text"},
                },
                {
                    "event_id": "$recent_reply",
                    "sender": "@agent:localhost",
                    "origin_server_ts": 9000,
                    "type": "m.room.message",
                    "content": {
                        "body": "Recent reply",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$thread_old_root_recent_reply",
                        },
                    },
                },
            ],
        )
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_recent_root_no_replies",
            events=[
                {
                    "event_id": "$thread_recent_root_no_replies",
                    "sender": "@user:localhost",
                    "origin_server_ts": 5000,
                    "type": "m.room.message",
                    "content": {"body": "Recent root", "msgtype": "m.text"},
                },
            ],
        )
        await _seed_thread_cache(
            cache,
            room_id="!other_room:localhost",
            thread_id="$thread_other_room",
            events=[
                {
                    "event_id": "$thread_other_room",
                    "sender": "@user:localhost",
                    "origin_server_ts": 99999,
                    "type": "m.room.message",
                    "content": {"body": "Other room root", "msgtype": "m.text"},
                },
            ],
        )

        all_recent = await cache.get_recent_room_thread_ids("!room:localhost", limit=10)
        first_only = await cache.get_recent_room_thread_ids("!room:localhost", limit=1)
    finally:
        await cache.close()

    assert all_recent == [
        "$thread_old_root_recent_reply",
        "$thread_recent_root_no_replies",
    ]
    assert first_only == ["$thread_old_root_recent_reply"]


@pytest.mark.asyncio
async def test_event_cache_preserves_insertion_order_for_same_timestamp_events(tmp_path: Path) -> None:
    """Cached reads should preserve the stored order when timestamps tie."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[
                {
                    "event_id": "$thread_root",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "type": "m.room.message",
                    "content": {"body": "Root message", "msgtype": "m.text"},
                },
                {
                    "event_id": "$zzz_parent",
                    "sender": "@user:localhost",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "body": "Parent",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$thread_root"}},
                    },
                },
                {
                    "event_id": "$aaa_child",
                    "sender": "@user:localhost",
                    "origin_server_ts": 2000,
                    "type": "m.room.message",
                    "content": {
                        "body": "Child",
                        "msgtype": "m.text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$zzz_parent"}},
                    },
                },
            ],
        )

        cached_events = await cache.get_thread_events("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert cached_events is not None
    assert [event["event_id"] for event in cached_events] == [
        "$thread_root",
        "$zzz_parent",
        "$aaa_child",
    ]


@pytest.mark.asyncio
async def test_individual_event_cache_store_and_retrieve(tmp_path: Path) -> None:
    """Individually cached events should round-trip by event ID."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    try:
        await cache.store_events_batch(
            [
                (
                    "$reply",
                    "!room:localhost",
                    {
                        "event_id": "$reply",
                        "sender": "@agent:localhost",
                        "origin_server_ts": 2000,
                        "type": "m.room.message",
                        "content": {"body": "Reply in thread", "msgtype": "m.text"},
                    },
                ),
            ],
        )

        cached_event = await cache.get_event("!room:localhost", "$reply")
        missing_event = await cache.get_event("!room:localhost", "$missing")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cached_event["content"]["body"] == "Reply in thread"
    assert missing_event is None


def test_event_cache_room_lock_cache_evicts_idle_rooms(tmp_path: Path) -> None:
    """Idle per-room locks should be evicted instead of growing without bound."""
    runtime = event_cache_module.SqliteEventCacheRuntime(tmp_path / "event_cache.db")

    for index in range(event_cache_module._MAX_CACHED_ROOM_LOCKS + 8):
        _ = runtime.room_lock_entry(f"!room-{index}:localhost").lock

    assert len(runtime.room_locks) == event_cache_module._MAX_CACHED_ROOM_LOCKS
    assert "!room-0:localhost" not in runtime.room_locks


@pytest.mark.asyncio
async def test_event_cache_room_lock_cache_keeps_contended_room_waiters(tmp_path: Path) -> None:
    """Queued waiters must keep a room lock alive across pruning churn."""
    runtime = event_cache_module.SqliteEventCacheRuntime(tmp_path / "event_cache.db")
    room_id = "!busy:localhost"
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    pruned_after_release = asyncio.Event()
    allow_waiter_exit = asyncio.Event()
    waiter_acquired = asyncio.Event()
    post_release_snapshot: dict[str, object] = {}

    async def first_holder() -> None:
        async with runtime.acquire_room_lock(room_id, operation="first_holder"):
            holder_entered.set()
            await release_holder.wait()
        for index in range(event_cache_module._MAX_CACHED_ROOM_LOCKS + 8):
            _ = runtime.room_lock_entry(f"!churn-{index}:localhost").lock
        entry = runtime.room_locks.get(room_id)
        post_release_snapshot["room_present"] = entry is not None
        post_release_snapshot["active_users"] = entry.active_users if entry is not None else None
        post_release_snapshot["lock_locked"] = entry.lock.locked() if entry is not None else None
        pruned_after_release.set()

    async def queued_waiter() -> None:
        async with runtime.acquire_room_lock(room_id, operation="queued_waiter"):
            waiter_acquired.set()
            await allow_waiter_exit.wait()

    async def wait_for_waiter_registration() -> None:
        loop = asyncio.get_running_loop()
        waiter_registered = loop.create_future()

        def check_waiter_registration() -> None:
            if runtime.room_locks[room_id].active_users >= 2:
                waiter_registered.set_result(None)
                return
            loop.call_soon(check_waiter_registration)

        loop.call_soon(check_waiter_registration)
        await asyncio.wait_for(waiter_registered, timeout=1.0)

    holder_task = asyncio.create_task(first_holder())
    waiter_task = asyncio.create_task(queued_waiter())

    await asyncio.wait_for(holder_entered.wait(), timeout=1.0)
    await wait_for_waiter_registration()

    busy_lock = runtime.room_lock_entry(room_id).lock
    release_holder.set()
    await asyncio.wait_for(pruned_after_release.wait(), timeout=1.0)

    assert post_release_snapshot == {
        "room_present": True,
        "active_users": 1,
        "lock_locked": False,
    }
    assert runtime.room_lock_entry(room_id).lock is busy_lock

    await asyncio.wait_for(waiter_acquired.wait(), timeout=1.0)
    allow_waiter_exit.set()
    await asyncio.gather(holder_task, waiter_task)


@pytest.mark.asyncio
async def test_event_cache_room_lock_cache_keeps_new_active_room_at_capacity(tmp_path: Path) -> None:
    """A newly acquired room lock must survive pruning when the cache is already full of active rooms."""
    runtime = event_cache_module.SqliteEventCacheRuntime(tmp_path / "event_cache.db")
    release_active_rooms = asyncio.Event()
    active_rooms_registered = asyncio.Event()
    release_new_room_holder = asyncio.Event()
    new_room_holder_entered = asyncio.Event()
    new_room_waiter_acquired = asyncio.Event()
    active_room_count = 0
    new_room_id = "!new-room:localhost"

    async def hold_active_room(room_id: str) -> None:
        nonlocal active_room_count
        async with runtime.acquire_room_lock(room_id, operation="hold_active_room"):
            active_room_count += 1
            if active_room_count == event_cache_module._MAX_CACHED_ROOM_LOCKS:
                active_rooms_registered.set()
            await release_active_rooms.wait()

    async def hold_new_room() -> None:
        async with runtime.acquire_room_lock(new_room_id, operation="hold_new_room"):
            new_room_holder_entered.set()
            await release_new_room_holder.wait()

    async def wait_for_new_room() -> None:
        async with runtime.acquire_room_lock(new_room_id, operation="wait_for_new_room"):
            new_room_waiter_acquired.set()

    active_room_tasks = [
        asyncio.create_task(hold_active_room(f"!active-room-{index}:localhost"))
        for index in range(event_cache_module._MAX_CACHED_ROOM_LOCKS)
    ]
    new_room_holder_task: asyncio.Task[None] | None = None
    new_room_waiter_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(active_rooms_registered.wait(), timeout=1.0)

        new_room_holder_task = asyncio.create_task(hold_new_room())
        await asyncio.wait_for(new_room_holder_entered.wait(), timeout=1.0)

        new_room_waiter_task = asyncio.create_task(wait_for_new_room())
        await asyncio.sleep(0)

        assert new_room_waiter_acquired.is_set() is False

        release_new_room_holder.set()
        await asyncio.wait_for(new_room_waiter_acquired.wait(), timeout=1.0)
    finally:
        release_new_room_holder.set()
        release_active_rooms.set()
        await asyncio.gather(
            *active_room_tasks,
            *(task for task in (new_room_holder_task, new_room_waiter_task) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_event_cache_close_waits_for_in_flight_operation(tmp_path: Path) -> None:
    """Closing the cache should wait for active DB work instead of closing mid-query."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    await cache.store_event(
        "$reply",
        "!room:localhost",
        {
            "event_id": "$reply",
            "sender": "@agent:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {"body": "Cached reply", "msgtype": "m.text"},
        },
    )
    operation_started = asyncio.Event()
    allow_operation_finish = asyncio.Event()
    original_load_event = sqlite_event_cache_events.load_event

    async def blocking_load_event(
        db: object,
        *,
        event_id: str,
    ) -> dict[str, object] | None:
        operation_started.set()
        await allow_operation_finish.wait()
        return await original_load_event(db, event_id=event_id)

    try:
        with patch(
            "mindroom.matrix.cache.sqlite_event_cache_events.load_event",
            new=blocking_load_event,
        ):
            get_task = asyncio.create_task(cache.get_event("!room:localhost", "$reply"))
            await asyncio.wait_for(operation_started.wait(), timeout=1.0)

            close_task = asyncio.create_task(cache.close())
            await asyncio.sleep(0)
            assert close_task.done() is False

            allow_operation_finish.set()
            cached_event = await get_task
            await close_task
    finally:
        if cache.is_initialized:
            await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cache.is_initialized is False


@pytest.mark.asyncio
async def test_event_cache_initialize_clears_half_initialized_connection_on_failure(tmp_path: Path) -> None:
    """Mid-init failures must close and clear the SQLite connection so a later retry can recover."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    broken_connection = AsyncMock()
    broken_connection.close = AsyncMock()
    broken_connection.execute = AsyncMock(side_effect=[MagicMock(), RuntimeError("pragma boom")])

    with (
        patch(
            "mindroom.matrix.cache.sqlite_event_cache.aiosqlite.connect",
            AsyncMock(return_value=broken_connection),
        ),
        pytest.raises(RuntimeError, match="pragma boom"),
    ):
        await cache.initialize()

    broken_connection.close.assert_awaited_once()
    assert cache.is_initialized is False


@pytest.mark.asyncio
async def test_individual_event_cache_strips_runtime_timing_marker(tmp_path: Path) -> None:
    """Batch event caching should drop in-memory timing objects before serialization."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Cached reply",
        server_timestamp=2000,
        source_content={"body": "Cached reply"},
    )
    event_source = _cache_source(reply_event)
    event_source["com.mindroom.dispatch_pipeline_timing"] = DispatchPipelineTiming(
        source_event_id="$reply",
        room_id="!room:localhost",
    )

    try:
        await cache.store_events_batch([("$reply", "!room:localhost", event_source)])
        cached_event = await cache.get_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert "com.mindroom.dispatch_pipeline_timing" not in cached_event


@pytest.mark.asyncio
async def test_thread_cache_store_populates_individual_event_lookup(tmp_path: Path) -> None:
    """Thread cache writes should also populate the individual event table."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Cached reply",
        server_timestamp=2000,
        source_content={
            "body": "Cached reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(reply_event)],
        )
        cached_event = await cache.get_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cached_event["content"]["body"] == "Cached reply"


@pytest.mark.asyncio
async def test_thread_event_cache_strips_runtime_timing_marker(tmp_path: Path) -> None:
    """Thread cache writes should strip runtime-only timing markers before JSON storage."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Reply in thread",
        server_timestamp=2000,
        source_content={
            "body": "Reply in thread",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    event_source = _cache_source(reply_event)
    event_source["com.mindroom.dispatch_pipeline_timing"] = DispatchPipelineTiming(
        source_event_id="$reply",
        room_id="!room:localhost",
    )

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[event_source],
        )
        cached_event = await cache.get_event("!room:localhost", "$reply")
        cached_thread_events = await cache.get_thread_events("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert cached_event is not None
    assert "com.mindroom.dispatch_pipeline_timing" not in cached_event
    assert cached_thread_events is not None
    assert "com.mindroom.dispatch_pipeline_timing" not in cached_thread_events[0]


@pytest.mark.asyncio
async def test_cached_room_get_event_cache_hit_avoids_network_call(tmp_path: Path) -> None:
    """Cached room get event lookups should reconstruct nio responses without I/O."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Cached reply",
        server_timestamp=2000,
        source_content={"body": "Cached reply"},
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()

    try:
        await cache.store_event("$reply", "!room:localhost", _cache_source(reply_event))
        response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Cached reply"
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_room_get_event_cache_hit_returns_latest_visible_edit(tmp_path: Path) -> None:
    """Point-event cache hits should surface the latest edited content for originals."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={
            "body": "Original reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    edit_event = _make_text_event(
        event_id="$reply_edit",
        sender="@agent:localhost",
        body="* Final reply",
        server_timestamp=3000,
        source_content={
            "body": "* Final reply",
            "m.new_content": {"body": "Final reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()

    try:
        await cache.store_events_batch(
            [
                ("$reply", "!room:localhost", _cache_source(original_event)),
                ("$reply_edit", "!room:localhost", _cache_source(edit_event)),
            ],
        )
        response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Final reply"
    assert response.event.server_timestamp == 3000
    assert EventInfo.from_event(response.event.source).thread_id == "$thread_root"
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_room_get_event_network_fetch_merges_cached_latest_edit(tmp_path: Path) -> None:
    """Network fetches should still project originals through cached latest edits."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply"},
    )
    edit_event = _make_text_event(
        event_id="$reply_edit",
        sender="@agent:localhost",
        body="* Final reply",
        server_timestamp=3000,
        source_content={
            "body": "* Final reply",
            "m.new_content": {"body": "Final reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(original_event))

    try:
        await cache.store_event("$reply_edit", "!room:localhost", _cache_source(edit_event))
        response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


@pytest.mark.asyncio
async def test_redacting_latest_edit_falls_back_to_previous_cached_edit(tmp_path: Path) -> None:
    """Removing the newest edit should expose the previous cached visible state."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=1000,
        source_content={"body": "Original reply"},
    )
    older_edit = _make_text_event(
        event_id="$reply_edit_1",
        sender="@agent:localhost",
        body="* Intermediate reply",
        server_timestamp=2000,
        source_content={
            "body": "* Intermediate reply",
            "m.new_content": {"body": "Intermediate reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    newer_edit = _make_text_event(
        event_id="$reply_edit_2",
        sender="@agent:localhost",
        body="* Final reply",
        server_timestamp=3000,
        source_content={
            "body": "* Final reply",
            "m.new_content": {"body": "Final reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()

    try:
        await cache.store_events_batch(
            [
                ("$reply", "!room:localhost", _cache_source(original_event)),
                ("$reply_edit_1", "!room:localhost", _cache_source(older_edit)),
                ("$reply_edit_2", "!room:localhost", _cache_source(newer_edit)),
            ],
        )
        latest_response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
        redacted = await cache.redact_event("!room:localhost", "$reply_edit_2")
        fallback_response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert redacted is True
    assert isinstance(latest_response, nio.RoomGetEventResponse)
    assert latest_response.event.body == "Final reply"
    assert isinstance(fallback_response, nio.RoomGetEventResponse)
    assert fallback_response.event.body == "Intermediate reply"
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_redaction_removes_individual_event_cache_entry(tmp_path: Path) -> None:
    """Redactions should also remove individually cached events."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Cached reply",
        server_timestamp=2000,
        source_content={
            "body": "Cached reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(reply_event)],
        )
        assert await cache.get_event("!room:localhost", "$reply") is not None
        redacted = await cache.redact_event("!room:localhost", "$reply")
        cached_event = await cache.get_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    assert redacted is True
    assert cached_event is None


@pytest.mark.asyncio
async def test_redacting_original_removes_dependent_cached_edits_from_thread_history(tmp_path: Path) -> None:
    """Redacting an original must also remove cached edits that would resurrect it."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={
            "body": "Original reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    edit_event = _make_text_event(
        event_id="$reply_edit",
        sender="@agent:localhost",
        body="* Final reply",
        server_timestamp=3000,
        source_content={
            "body": "* Final reply",
            "m.new_content": {
                "body": "Final reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()
    client.room_messages = AsyncMock(return_value=nio.RoomMessagesResponse([], None, None, None))
    client.room_get_event_relations = MagicMock()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(original_event), _cache_source(edit_event)],
        )
        history_before = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)

        redacted = await cache.redact_event("!room:localhost", "$reply")
        latest_edit = await cache.get_latest_edit("!room:localhost", "$reply")
        cached_edit = await cache.get_event("!room:localhost", "$reply_edit")
        history_after = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)
    finally:
        await cache.close()

    assert redacted is True
    assert [(message.event_id, message.body) for message in history_before] == [
        ("$thread_root", "Root message"),
        ("$reply", "Final reply"),
    ]
    assert latest_edit is None
    assert cached_edit is None
    assert [(message.event_id, message.body) for message in history_after] == [
        ("$thread_root", "Root message"),
    ]


@pytest.mark.asyncio
async def test_invalidate_thread_preserves_separately_cached_latest_edit(tmp_path: Path) -> None:
    """Thread invalidation should not sever edit projection for separately cached edits."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={
            "body": "Original reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    edit_event = _make_text_event(
        event_id="$reply_edit",
        sender="@agent:localhost",
        body="* Final reply",
        server_timestamp=3000,
        source_content={
            "body": "* Final reply",
            "m.new_content": {"body": "Final reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(original_event))

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(original_event)],
        )
        await cache.store_event("$reply_edit", "!room:localhost", _cache_source(edit_event))
        await cache.invalidate_thread("!room:localhost", "$thread_root")

        latest_edit = await cache.get_latest_edit("!room:localhost", "$reply")
        response, _ = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$reply_edit"
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


@pytest.mark.asyncio
async def test_invalidate_thread_removes_event_thread_rows(tmp_path: Path) -> None:
    """Thread invalidation must also clear durable event-to-thread mappings."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Reply",
        server_timestamp=2000,
        source_content={
            "body": "Reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(reply_event)],
        )
        assert await cache.get_thread_id_for_event("!room:localhost", "$reply") == "$thread_root"

        await cache.invalidate_thread("!room:localhost", "$thread_root")
        thread_id = await cache.get_thread_id_for_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    assert thread_id is None


@pytest.mark.asyncio
async def test_redaction_removes_event_thread_rows_and_blocks_late_edit_resurrection(tmp_path: Path) -> None:
    """Redacting a reply must clear durable thread mapping and ignore late edits for that reply."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Reply",
        server_timestamp=2000,
        source_content={
            "body": "Reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    late_edit_event = _make_text_event(
        event_id="$reply_edit",
        sender="@agent:localhost",
        body="* Reply edited",
        server_timestamp=3000,
        source_content={
            "body": "* Reply edited",
            "m.new_content": {
                "body": "Reply edited",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()
    client.room_messages = AsyncMock(return_value=nio.RoomMessagesResponse([], None, None, None))
    client.room_get_event_relations = MagicMock()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(reply_event)],
        )
        assert await cache.get_thread_id_for_event("!room:localhost", "$reply") == "$thread_root"

        redacted = await cache.redact_event("!room:localhost", "$reply")
        await cache.store_events_batch([("$reply_edit", "!room:localhost", _cache_source(late_edit_event))])

        thread_id = await cache.get_thread_id_for_event("!room:localhost", "$reply")
        cached_late_edit = await cache.get_event("!room:localhost", "$reply_edit")
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)
    finally:
        await cache.close()

    assert redacted is True
    assert thread_id is None
    assert cached_late_edit is None
    assert [(message.event_id, message.body) for message in history] == [
        ("$thread_root", "Root message"),
    ]


@pytest.mark.asyncio
async def test_store_events_batch_records_thread_root_self_mapping_from_explicit_thread_child(
    tmp_path: Path,
) -> None:
    """Explicit threaded children should also make the root resolve to its own thread id."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    reply_event = _make_text_event(
        event_id="$reply",
        sender="@user:localhost",
        body="Reply",
        server_timestamp=2000,
        source_content={
            "body": "Reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )

    try:
        await cache.store_events_batch([("$reply", "!room:localhost", _cache_source(reply_event))])
        reply_thread_id = await cache.get_thread_id_for_event("!room:localhost", "$reply")
        root_thread_id = await cache.get_thread_id_for_event("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert reply_thread_id == "$thread_root"
    assert root_thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_store_events_batch_rolls_back_on_index_derivation_failure(tmp_path: Path) -> None:
    """Failed batch writes must not leak partial point-lookup rows into later commits."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    valid_event = _cache_source(
        _make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Reply",
            server_timestamp=2000,
            source_content={"body": "Reply"},
        ),
    )
    invalid_edit_event = {
        "event_id": "$reply_edit",
        "sender": "@agent:localhost",
        "type": "m.room.message",
        "content": {
            "body": "* Reply edited",
            "m.new_content": {"body": "Reply edited", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    }
    later_event = _cache_source(
        _make_text_event(
            event_id="$later",
            sender="@agent:localhost",
            body="Later",
            server_timestamp=4000,
            source_content={"body": "Later"},
        ),
    )

    try:
        with pytest.raises(ValueError, match="origin_server_ts"):
            await cache.store_events_batch(
                [
                    ("$reply", "!room:localhost", valid_event),
                    ("$reply_edit", "!room:localhost", invalid_edit_event),
                ],
            )

        await cache.store_events_batch([("$later", "!room:localhost", later_event)])
        cached_reply = await cache.get_event("!room:localhost", "$reply")
        cached_invalid_edit = await cache.get_event("!room:localhost", "$reply_edit")
        cached_later = await cache.get_event("!room:localhost", "$later")
    finally:
        await cache.close()

    assert cached_reply is None
    assert cached_invalid_edit is None
    assert cached_later is not None
    assert cached_later["event_id"] == "$later"


@pytest.mark.asyncio
async def test_initialize_resets_stale_old_cache_schema(tmp_path: Path) -> None:
    """Initialization should discard stale cache DBs instead of migrating them forward."""
    db_path = tmp_path / "event_cache.db"
    original_event = _cache_source(
        _make_text_event(
            event_id="$reply",
            sender="@agent:localhost",
            body="Original reply",
            server_timestamp=2000,
            source_content={"body": "Original reply"},
        ),
    )
    edit_event = _cache_source(
        _make_text_event(
            event_id="$reply_edit",
            sender="@agent:localhost",
            body="* Final reply",
            server_timestamp=3000,
            source_content={
                "body": "* Final reply",
                "m.new_content": {"body": "Final reply", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        ),
    )

    with closing(sqlite3.connect(db_path)) as db:
        db.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                event_json TEXT NOT NULL,
                cached_at REAL NOT NULL
            )
            """,
        )
        db.executemany(
            """
            INSERT INTO events(event_id, room_id, event_json, cached_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("$reply", "!room:localhost", json.dumps(original_event, separators=(",", ":")), 1.0),
                ("$reply_edit", "!room:localhost", json.dumps(edit_event, separators=(",", ":")), 1.0),
            ],
        )
        db.commit()

    cache = SqliteEventCache(db_path)
    await cache.initialize()
    try:
        latest_edit = await cache.get_latest_edit("!room:localhost", "$reply")
        cached_original = await cache.get_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    with closing(sqlite3.connect(db_path)) as db:
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]

    assert latest_edit is None
    assert cached_original is None
    assert schema_version == event_cache_module.EVENT_CACHE_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_disabled_event_cache_skips_latest_agent_message_snapshot_reads(tmp_path: Path) -> None:
    """Disabled caches should fail open for latest-agent-message snapshot reads."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$reply",
                    "!room:localhost",
                    {
                        "event_id": "$reply",
                        "sender": "@agent:localhost",
                        "origin_server_ts": 2000,
                        "type": "m.room.message",
                        "content": {"body": "Working...", "msgtype": "m.text"},
                    },
                ),
            ],
        )
        cache.disable("test_disabled")

        snapshot = await cache.get_latest_agent_message_snapshot(
            "!room:localhost",
            None,
            "@agent:localhost",
            runtime_started_at=0.0,
        )
    finally:
        await cache.close()

    assert snapshot is None


@pytest.mark.asyncio
async def test_fetch_thread_history_cache_hit_avoids_full_fetch_calls(tmp_path: Path) -> None:
    """Cache hits should bypass the full root-plus-relations fetch path."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Cached reply",
        server_timestamp=2000,
        source_content={
            "body": "Cached reply",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    await _seed_thread_cache(
        cache,
        room_id="!room:localhost",
        thread_id="$thread_root",
        events=[_cache_source(root_event), _cache_source(reply_event)],
    )

    client = MagicMock()
    incremental_page = MagicMock(spec=nio.RoomMessagesResponse)
    incremental_page.chunk = [reply_event, root_event]
    incremental_page.end = None
    client.room_messages = AsyncMock(return_value=incremental_page)
    client.room_get_event = AsyncMock()
    client.room_get_event_relations = MagicMock()

    try:
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)
    finally:
        await cache.close()

    assert [message.event_id for message in history] == ["$thread_root", "$reply"]
    client.room_get_event.assert_not_awaited()
    client.room_get_event_relations.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_thread_history_cache_miss_does_full_fetch(tmp_path: Path) -> None:
    """Cache misses should scan room history and populate the cache."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    reply_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Reply in thread",
        server_timestamp=2000,
        source_content={
            "body": "Reply in thread",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    client = _make_relations_client(
        root_event=root_event,
        relations={
            _relation_key("$thread_root", RelationshipType.thread): [reply_event],
            _relation_key("$thread_root", RelationshipType.replacement): [],
            _relation_key("$reply", RelationshipType.replacement): [],
        },
    )

    try:
        history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=cache)
        cached_events = await cache.get_thread_events("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert [message.event_id for message in history] == ["$thread_root", "$reply"]
    assert cached_events is not None
    assert [event["event_id"] for event in cached_events] == ["$thread_root", "$reply"]
    client.room_get_event.assert_not_awaited()
    client.room_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_mxc_text_cache_round_trips_across_event_cache_reopen(tmp_path: Path) -> None:
    """Durable MXC text rows should survive closing and reopening the event cache."""
    db_path = tmp_path / "event_cache.db"
    cache = SqliteEventCache(db_path)
    await cache.initialize()

    try:
        await cache.store_mxc_text("!room:localhost", "mxc://server/sidecar", "Full text sidecar")
    finally:
        await cache.close()

    reopened_cache = SqliteEventCache(db_path)
    await reopened_cache.initialize()
    try:
        cached_text = await reopened_cache.get_mxc_text("!room:localhost", "mxc://server/sidecar")
    finally:
        await reopened_cache.close()

    assert cached_text == "Full text sidecar"


@pytest.mark.asyncio
async def test_fetch_thread_history_reuses_durable_mxc_text_after_restart(tmp_path: Path) -> None:
    """Cached full-history reads should reuse durable sidecar text after a restart."""
    db_path = tmp_path / "event_cache.db"
    cache = SqliteEventCache(db_path)
    await cache.initialize()
    _clear_mxc_cache()

    root_event = _make_text_event(
        event_id="$thread_root",
        sender="@user:localhost",
        body="Root message",
        server_timestamp=1000,
        source_content={"body": "Root message"},
    )
    sidecar_reply = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Preview reply",
        server_timestamp=2000,
        source_content={
            "body": "Preview reply",
            "msgtype": "m.file",
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
            "url": "mxc://server/sidecar",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    )
    canonical_sidecar_content = {"body": "Full reply", "msgtype": "m.text"}

    first_client = MagicMock()
    first_client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(canonical_sidecar_content).encode("utf-8"),
        ),
    )
    first_client.room_get_event = AsyncMock()
    first_client.room_messages = AsyncMock()
    first_client.room_get_event_relations = MagicMock()

    try:
        await _seed_thread_cache(
            cache,
            room_id="!room:localhost",
            thread_id="$thread_root",
            events=[_cache_source(root_event), _cache_source(sidecar_reply)],
        )

        first_history = await fetch_thread_history(first_client, "!room:localhost", "$thread_root", event_cache=cache)
    finally:
        await cache.close()

    _clear_mxc_cache()

    reopened_cache = SqliteEventCache(db_path)
    await reopened_cache.initialize()
    second_client = MagicMock()
    second_client.download = AsyncMock(
        return_value=MagicMock(spec=nio.DownloadError),
    )
    second_client.room_get_event = AsyncMock()
    second_client.room_messages = AsyncMock()
    second_client.room_get_event_relations = MagicMock()

    try:
        second_history = await fetch_thread_history(
            second_client,
            "!room:localhost",
            "$thread_root",
            event_cache=reopened_cache,
        )
    finally:
        await reopened_cache.close()
        _clear_mxc_cache()

    assert [message.body for message in first_history] == ["Root message", "Full reply"]
    assert [message.body for message in second_history] == ["Root message", "Full reply"]
    first_client.download.assert_awaited_once_with(mxc="mxc://server/sidecar")
    second_client.download.assert_not_awaited()


def test_event_cache_uses_distinct_locks_per_room(tmp_path: Path) -> None:
    """Event cache should keep independent locks per room."""
    runtime = event_cache_module.SqliteEventCacheRuntime(tmp_path / "event_cache.db")

    assert runtime.room_lock_entry("!room:localhost").lock is runtime.room_lock_entry("!room:localhost").lock
    assert runtime.room_lock_entry("!room:localhost").lock is not runtime.room_lock_entry("!other:localhost").lock
