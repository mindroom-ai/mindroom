"""Tests for the SQLite-backed Matrix thread event cache."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.api import RelationshipType

import mindroom.matrix._event_cache as event_cache_module
from mindroom.matrix._event_cache import _EventCache
from mindroom.matrix.client import fetch_thread_history
from mindroom.matrix.conversation_cache import _cached_room_get_event as cached_room_get_event
from mindroom.matrix.event_info import EventInfo
from mindroom.timing import DispatchPipelineTiming

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


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
    client.room_messages = AsyncMock()

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
    return client


@pytest.mark.asyncio
async def test_event_cache_store_and_retrieve(tmp_path: Path) -> None:
    """Stored events should round-trip in timestamp order."""
    cache = _EventCache(tmp_path / "event_cache.db")
    await cache.initialize()

    try:
        await cache.store_events(
            "!room:localhost",
            "$thread_root",
            [
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
        latest_ts = await cache.get_latest_ts("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert cached_events is not None
    assert [event["event_id"] for event in cached_events] == ["$thread_root", "$reply"]
    assert latest_ts == 2000


@pytest.mark.asyncio
async def test_individual_event_cache_store_and_retrieve(tmp_path: Path) -> None:
    """Individually cached events should round-trip by event ID."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
    cache = _EventCache(tmp_path / "event_cache.db")

    for index in range(event_cache_module._MAX_CACHED_ROOM_LOCKS + 8):
        cache._room_lock(f"!room-{index}:localhost")

    assert len(cache._room_locks) == event_cache_module._MAX_CACHED_ROOM_LOCKS
    assert "!room-0:localhost" not in cache._room_locks


@pytest.mark.asyncio
async def test_event_cache_room_lock_cache_keeps_contended_room_waiters(tmp_path: Path) -> None:
    """Queued waiters must keep a room lock alive across pruning churn."""
    cache = _EventCache(tmp_path / "event_cache.db")
    room_id = "!busy:localhost"
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    pruned_after_release = asyncio.Event()
    allow_waiter_exit = asyncio.Event()
    waiter_acquired = asyncio.Event()
    post_release_snapshot: dict[str, object] = {}

    async def first_holder() -> None:
        async with cache._acquire_room_lock(room_id, operation="first_holder"):
            holder_entered.set()
            await release_holder.wait()
        for index in range(event_cache_module._MAX_CACHED_ROOM_LOCKS + 8):
            cache._room_lock(f"!churn-{index}:localhost")
        entry = cache._room_locks.get(room_id)
        post_release_snapshot["room_present"] = entry is not None
        post_release_snapshot["active_users"] = entry.active_users if entry is not None else None
        post_release_snapshot["lock_locked"] = entry.lock.locked() if entry is not None else None
        pruned_after_release.set()

    async def queued_waiter() -> None:
        async with cache._acquire_room_lock(room_id, operation="queued_waiter"):
            waiter_acquired.set()
            await allow_waiter_exit.wait()

    async def wait_for_waiter_registration() -> None:
        loop = asyncio.get_running_loop()
        waiter_registered = loop.create_future()

        def check_waiter_registration() -> None:
            if cache._room_locks[room_id].active_users >= 2:
                waiter_registered.set_result(None)
                return
            loop.call_soon(check_waiter_registration)

        loop.call_soon(check_waiter_registration)
        await asyncio.wait_for(waiter_registered, timeout=1.0)

    holder_task = asyncio.create_task(first_holder())
    waiter_task = asyncio.create_task(queued_waiter())

    await asyncio.wait_for(holder_entered.wait(), timeout=1.0)
    await wait_for_waiter_registration()

    busy_lock = cache._room_lock(room_id)
    release_holder.set()
    await asyncio.wait_for(pruned_after_release.wait(), timeout=1.0)

    assert post_release_snapshot == {
        "room_present": True,
        "active_users": 1,
        "lock_locked": False,
    }
    assert cache._room_lock(room_id) is busy_lock

    await asyncio.wait_for(waiter_acquired.wait(), timeout=1.0)
    allow_waiter_exit.set()
    await asyncio.gather(holder_task, waiter_task)


@pytest.mark.asyncio
async def test_event_cache_room_lock_cache_keeps_new_active_room_at_capacity(tmp_path: Path) -> None:
    """A newly acquired room lock must survive pruning when the cache is already full of active rooms."""
    cache = _EventCache(tmp_path / "event_cache.db")
    release_active_rooms = asyncio.Event()
    active_rooms_registered = asyncio.Event()
    release_new_room_holder = asyncio.Event()
    new_room_holder_entered = asyncio.Event()
    new_room_waiter_acquired = asyncio.Event()
    active_room_count = 0
    new_room_id = "!new-room:localhost"

    async def hold_active_room(room_id: str) -> None:
        nonlocal active_room_count
        async with cache._acquire_room_lock(room_id, operation="hold_active_room"):
            active_room_count += 1
            if active_room_count == event_cache_module._MAX_CACHED_ROOM_LOCKS:
                active_rooms_registered.set()
            await release_active_rooms.wait()

    async def hold_new_room() -> None:
        async with cache._acquire_room_lock(new_room_id, operation="hold_new_room"):
            new_room_holder_entered.set()
            await release_new_room_holder.wait()

    async def wait_for_new_room() -> None:
        async with cache._acquire_room_lock(new_room_id, operation="wait_for_new_room"):
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
    assert cache._db is not None

    operation_started = asyncio.Event()
    allow_operation_finish = asyncio.Event()
    original_execute = cache._db.execute

    async def blocking_execute(*args: object, **kwargs: object) -> object:
        operation_started.set()
        await allow_operation_finish.wait()
        return await original_execute(*args, **kwargs)

    cache._db.execute = blocking_execute

    try:
        get_task = asyncio.create_task(cache.get_event("!room:localhost", "$reply"))
        await asyncio.wait_for(operation_started.wait(), timeout=1.0)

        close_task = asyncio.create_task(cache.close())
        await asyncio.sleep(0)
        assert close_task.done() is False

        allow_operation_finish.set()
        cached_event = await get_task
        await close_task
    finally:
        if cache._db is not None:
            await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cache._db is None


@pytest.mark.asyncio
async def test_individual_event_cache_strips_runtime_timing_marker(tmp_path: Path) -> None:
    """Batch event caching should drop in-memory timing objects before serialization."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        await cache.store_events(
            "!room:localhost",
            "$thread_root",
            [_cache_source(root_event), _cache_source(reply_event)],
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        await cache.store_events("!room:localhost", "$thread_root", [event_source])
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Cached reply"
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_room_get_event_cache_hit_returns_latest_visible_edit(tmp_path: Path) -> None:
    """Point-event cache hits should surface the latest edited content for originals."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
        response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


@pytest.mark.asyncio
async def test_redacting_latest_edit_falls_back_to_previous_cached_edit(tmp_path: Path) -> None:
    """Removing the newest edit should expose the previous cached visible state."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
        latest_response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
        redacted = await cache.redact_event("!room:localhost", "$reply_edit_2")
        fallback_response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        await cache.store_events(
            "!room:localhost",
            "$thread_root",
            [_cache_source(root_event), _cache_source(reply_event)],
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        await cache.store_events(
            "!room:localhost",
            "$thread_root",
            [_cache_source(root_event), _cache_source(original_event), _cache_source(edit_event)],
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
    cache = _EventCache(tmp_path / "event_cache.db")
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
        await cache.store_events(
            "!room:localhost",
            "$thread_root",
            [_cache_source(root_event), _cache_source(original_event)],
        )
        await cache.store_event("$reply_edit", "!room:localhost", _cache_source(edit_event))
        await cache.invalidate_thread("!room:localhost", "$thread_root")

        latest_edit = await cache.get_latest_edit("!room:localhost", "$reply")
        response = await cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$reply_edit"
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


@pytest.mark.asyncio
async def test_initialize_backfills_event_edit_index_from_old_schema(tmp_path: Path) -> None:
    """Initialization should rebuild the edit index for pre-event_edits cache databases."""
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

    with sqlite3.connect(db_path) as db:
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

    cache = _EventCache(db_path)
    await cache.initialize()
    try:
        latest_edit = await cache.get_latest_edit("!room:localhost", "$reply")
    finally:
        await cache.close()

    with sqlite3.connect(db_path) as db:
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$reply_edit"
    assert latest_edit["content"]["m.new_content"]["body"] == "Final reply"
    assert schema_version == event_cache_module._EVENT_CACHE_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_pending_lookup_repairs_prune_stale_unmatched_entries(tmp_path: Path) -> None:
    """Old unmatched lookup repairs should age out instead of growing forever."""
    db_path = tmp_path / "event_cache.db"
    cache = _EventCache(db_path)
    await cache.initialize()
    try:
        stale_created_at = time.time() - event_cache_module._PENDING_LOOKUP_REPAIR_RETENTION_SECONDS - 1
        fresh_created_at = time.time()
        with sqlite3.connect(db_path) as db:
            db.executemany(
                """
                INSERT INTO pending_lookup_repairs(room_id, event_id, created_at)
                VALUES (?, ?, ?)
                """,
                [
                    ("!room:localhost", "$stale", stale_created_at),
                    ("!room:localhost", "$fresh", fresh_created_at),
                ],
            )
            db.commit()

        pending = await cache.pending_lookup_repairs_for_event_ids(
            "!room:localhost",
            frozenset({"$stale", "$fresh"}),
        )

        with sqlite3.connect(db_path) as db:
            remaining_repairs = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT event_id
                    FROM pending_lookup_repairs
                    WHERE room_id = ?
                    """,
                    ("!room:localhost",),
                ).fetchall()
            }
    finally:
        await cache.close()

    assert pending == frozenset({"$fresh"})
    assert remaining_repairs == {"$fresh"}


@pytest.mark.asyncio
async def test_fetch_thread_history_cache_hit_avoids_full_fetch_calls(tmp_path: Path) -> None:
    """Cache hits should bypass the full root-plus-relations fetch path."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
    await cache.store_events("!room:localhost", "$thread_root", [_cache_source(root_event), _cache_source(reply_event)])

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
    """Cache misses should fetch from the homeserver and populate the cache."""
    cache = _EventCache(tmp_path / "event_cache.db")
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
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$thread_root")
    client.room_messages.assert_not_awaited()


def test_event_cache_uses_distinct_locks_per_room(tmp_path: Path) -> None:
    """Event cache should keep independent locks per room."""
    cache = _EventCache(tmp_path / "event_cache.db")

    assert cache._room_lock("!room:localhost") is cache._room_lock("!room:localhost")
    assert cache._room_lock("!room:localhost") is not cache._room_lock("!other:localhost")


@pytest.mark.asyncio
async def test_fetch_thread_history_gracefully_falls_back_on_db_error() -> None:
    """Database errors should fall back to the homeserver fetch path."""
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
    broken_cache = MagicMock(spec=_EventCache)
    broken_cache.get_thread_events = AsyncMock(side_effect=RuntimeError("db broken"))
    broken_cache.invalidate_thread = AsyncMock()
    broken_cache.store_thread_events = AsyncMock()

    history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=broken_cache)

    assert [message.event_id for message in history] == ["$thread_root", "$reply"]
    broken_cache.get_thread_events.assert_awaited_once_with("!room:localhost", "$thread_root")
    assert broken_cache.invalidate_thread.await_count >= 1
    broken_cache.store_thread_events.assert_awaited_once()
