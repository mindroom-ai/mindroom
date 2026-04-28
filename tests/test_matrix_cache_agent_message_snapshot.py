"""Tests for latest-agent-message snapshot reads via the event cache API."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.matrix.cache import AgentMessageSnapshot, AgentMessageSnapshotUnavailable, ConversationEventCache

if TYPE_CHECKING:
    from collections.abc import Callable


def _message_event(
    *,
    event_id: str,
    sender: str,
    body: str,
    origin_server_ts: int,
    relates_to: dict[str, object] | None = None,
    new_content: dict[str, object] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "body": body,
        "msgtype": "m.text",
    }
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    if new_content is not None:
        content["m.new_content"] = {
            "msgtype": "m.text",
            **new_content,
        }
    return {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "type": "m.room.message",
        "content": content,
    }


async def _read_snapshot(
    cache_factory: Callable[[], ConversationEventCache],
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    cache = cache_factory()
    await cache.initialize()
    try:
        return await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            sender,
            runtime_started_at=runtime_started_at,
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_unedited_thread_message(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Thread-scope reads should return the latest unedited agent message."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Answer",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id="$thread-root",
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={
            "body": "Answer",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
        origin_server_ts=2000,
    )


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_streaming_status_for_threaded_message(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Edited threaded messages should surface the latest visible stream status."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Working...",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
                _message_event(
                    event_id="$reply-edit",
                    sender="@agent:localhost",
                    body="* Working...",
                    origin_server_ts=3000,
                    relates_to={"rel_type": "m.replace", "event_id": "$reply"},
                    new_content={
                        "body": "Still working",
                        "io.mindroom.stream_status": "streaming",
                    },
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id="$thread-root",
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={
            "body": "Still working",
            "msgtype": "m.text",
            "io.mindroom.stream_status": "streaming",
        },
        origin_server_ts=3000,
    )


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_room_level_message_when_thread_id_none(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should skip threaded replies and stay on the room timeline."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message",
                        sender="@agent:localhost",
                        body="Room timeline reply",
                        origin_server_ts=2000,
                    ),
                ),
            ],
        )
        await cache.store_events_batch(
            [
                (
                    "$thread-reply",
                    "!room:localhost",
                    _message_event(
                        event_id="$thread-reply",
                        sender="@agent:localhost",
                        body="Thread reply",
                        origin_server_ts=3000,
                        relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={"body": "Room timeline reply", "msgtype": "m.text"},
        origin_server_ts=2000,
    )


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_none_when_sender_has_no_message(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Missing sender matches should return None instead of raising."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message",
                        sender="@other:localhost",
                        body="Not the agent",
                        origin_server_ts=2000,
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_none_when_cache_has_no_rows(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Empty cache files should return None for any scope lookup."""
    cache = event_cache_factory()
    await cache.initialize()
    await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id="$thread-root",
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_room_scope_ignores_messages_cached_before_current_runtime(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should ignore stale message rows from a prior runtime."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message",
                        sender="@agent:localhost",
                        body="Working...",
                        origin_server_ts=2000,
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=time.time() + 1.0,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_room_scope_keeps_visible_edit_cached_in_current_runtime(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should keep a message whose visible edit was cached after restart."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message",
                        sender="@agent:localhost",
                        body="Working...",
                        origin_server_ts=2000,
                    ),
                ),
            ],
        )
        runtime_started_at = time.time()
        await cache.store_events_batch(
            [
                (
                    "$room-message-edit",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message-edit",
                        sender="@agent:localhost",
                        body="* Working...",
                        origin_server_ts=3000,
                        relates_to={"rel_type": "m.replace", "event_id": "$room-message"},
                        new_content={
                            "body": "Still working",
                            "io.mindroom.stream_status": "streaming",
                        },
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=runtime_started_at,
    )

    assert snapshot == AgentMessageSnapshot(
        content={
            "body": "Still working",
            "msgtype": "m.text",
            "io.mindroom.stream_status": "streaming",
        },
        origin_server_ts=3000,
    )


@pytest.mark.asyncio
async def test_room_scope_does_not_fall_back_to_older_fresh_message_when_latest_is_stale(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should fail closed when the latest sender message is stale."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$newer-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$newer-message",
                        sender="@agent:localhost",
                        body="Newest stale message",
                        origin_server_ts=2000,
                    ),
                ),
            ],
        )
        runtime_started_at = time.time()
        await cache.store_events_batch(
            [
                (
                    "$older-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$older-message",
                        sender="@agent:localhost",
                        body="Older fresh message",
                        origin_server_ts=1000,
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=runtime_started_at,
    )

    assert snapshot is None


@pytest.mark.asyncio
async def test_accessor_accepts_old_thread_cache_without_stale_marker(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Threaded reads should trust old snapshots unless a stale marker exists."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Working...",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
            ],
            validated_at=400.0,
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id="$thread-root",
        sender="@agent:localhost",
        runtime_started_at=100.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={
            "body": "Working...",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
        origin_server_ts=2000,
    )


@pytest.mark.asyncio
async def test_accessor_reuses_thread_cache_from_prior_bot_run(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Threaded reads should trust snapshots unless an explicit stale marker exists."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Working...",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
            ],
            validated_at=1000.0,
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id="$thread-root",
        sender="@agent:localhost",
        runtime_started_at=1001.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={
            "body": "Working...",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
        origin_server_ts=2000,
    )


@pytest.mark.asyncio
async def test_accessor_rejects_invalidated_thread_cache(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Threaded reads should fail closed after durable invalidation."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Working...",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
            ],
            validated_at=1000.0,
        )
        await cache.mark_thread_stale(
            "!room:localhost",
            "$thread-root",
            reason="test_invalidated",
        )
    finally:
        await cache.close()

    with pytest.raises(AgentMessageSnapshotUnavailable, match="thread_invalidated_after_validation"):
        await _read_snapshot(
            event_cache_factory,
            room_id="!room:localhost",
            thread_id="$thread-root",
            sender="@agent:localhost",
            runtime_started_at=0.0,
        )


@pytest.mark.asyncio
async def test_room_scope_returns_latest_by_origin_server_ts_not_cached_at(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should follow Matrix timeline order, not cache write time."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$room-message",
                    "!room:localhost",
                    _message_event(
                        event_id="$room-message",
                        sender="@agent:localhost",
                        body="Newest room message",
                        origin_server_ts=3000,
                    ),
                ),
            ],
        )
        await cache.replace_thread(
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@agent:localhost",
                    body="Older thread root",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$thread-reply",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
                ),
            ],
            validated_at=5000.0,
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={"body": "Newest room message", "msgtype": "m.text"},
        origin_server_ts=3000,
    )


@pytest.mark.asyncio
async def test_room_scope_preserves_cache_insert_order_for_same_timestamp_messages(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Room-scope reads should keep the later cached sender message when timestamps tie."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await cache.store_events_batch(
            [
                (
                    "$zzz-first",
                    "!room:localhost",
                    _message_event(
                        event_id="$zzz-first",
                        sender="@agent:localhost",
                        body="First cached message",
                        origin_server_ts=3000,
                    ),
                ),
            ],
        )
        await cache.store_events_batch(
            [
                (
                    "$aaa-second",
                    "!room:localhost",
                    _message_event(
                        event_id="$aaa-second",
                        sender="@agent:localhost",
                        body="Second cached message",
                        origin_server_ts=3000,
                    ),
                ),
            ],
        )
    finally:
        await cache.close()

    snapshot = await _read_snapshot(
        event_cache_factory,
        room_id="!room:localhost",
        thread_id=None,
        sender="@agent:localhost",
        runtime_started_at=0.0,
    )

    assert snapshot == AgentMessageSnapshot(
        content={"body": "Second cached message", "msgtype": "m.text"},
        origin_server_ts=3000,
    )
