"""Tests for latest-agent-message snapshot reads via the event cache API."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.matrix.cache import AgentMessageSnapshot, ConversationEventCache
from mindroom.matrix.cache.agent_message_snapshot import AgentMessageSnapshotUnavailable
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread

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


async def _overwrite_cached_event_payload(
    cache: ConversationEventCache,
    *,
    room_id: str,
    event_id: str,
    event: dict[str, Any],
) -> None:
    """Model one legacy row whose indexed room disagrees with its stored payload."""
    event_json = json.dumps(event, separators=(",", ":"))
    if isinstance(cache, SqliteEventCache):
        async with cache._runtime.acquire_db_operation() as db:
            await db.execute(
                """
                UPDATE events
                SET event_json = ?
                WHERE principal_id = ? AND room_id = ? AND event_id = ?
                """,
                (event_json, cache.principal_id, room_id, event_id),
            )
            await db.commit()
        return
    assert isinstance(cache, PostgresEventCache)
    async with cache._runtime.acquire_db_operation(operation="test_overwrite_cached_event_payload") as db:
        await db.execute(
            """
            UPDATE mindroom_event_cache_events
            SET event_json = %s
            WHERE namespace = %s AND room_id = %s AND event_id = %s
            """,
            (event_json, cache.namespace, room_id, event_id),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_unedited_thread_message(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Thread-scope reads should return the latest unedited agent message."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await _replace_thread(
            cache,
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
async def test_get_latest_agent_message_snapshot_keeps_agent_thread_root(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """The authoritative thread root remains eligible when the agent sent it."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@agent:localhost",
                    body="Agent question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@user:localhost",
                    body="User answer",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
            ],
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is not None
    assert snapshot.content["body"] == "Agent question"


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_ignores_wrong_thread_index_row(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A thread index cannot authorize a payload whose relation names another thread."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$reply",
                    sender="@agent:localhost",
                    body="Answer",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
            ],
        )
        await cache.mark_thread_stale(room_id, thread_id, reason="live_thread_mutation")
        assert await cache.append_event(
            room_id,
            thread_id,
            _message_event(
                event_id="$wrong",
                sender="@agent:localhost",
                body="Wrong thread",
                origin_server_ts=3000,
                relates_to={"rel_type": "m.thread", "event_id": "$other-root"},
            ),
        )
        assert await cache.revalidate_thread_after_incremental_update(room_id, thread_id)
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is not None
    assert snapshot.content["body"] == "Answer"


@pytest.mark.parametrize(
    "relation_type",
    [
        "reply",
        "reference",
    ],
    ids=["reply", "reference"],
)
@pytest.mark.parametrize(
    ("target_event_id", "expected_body"),
    [
        ("$direct-child", "Indirect answer"),
        ("$other-thread-child", "Direct answer"),
    ],
    ids=["same-thread", "other-thread"],
)
@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_resolves_indexed_indirect_thread_member(
    event_cache_factory: Callable[[], ConversationEventCache],
    relation_type: str,
    target_event_id: str,
    expected_body: str,
) -> None:
    """Replies and references must resolve through their canonical relation graph."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        indirect_relation = (
            {"m.in_reply_to": {"event_id": target_event_id}}
            if relation_type == "reply"
            else {"rel_type": "m.reference", "event_id": target_event_id}
        )
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$direct-answer",
                    sender="@agent:localhost",
                    body="Direct answer",
                    origin_server_ts=1500,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
                _message_event(
                    event_id="$direct-child",
                    sender="@user:localhost",
                    body="Direct child",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
                _message_event(
                    event_id="$other-thread-child",
                    sender="@user:localhost",
                    body="Other thread child",
                    origin_server_ts=2500,
                    relates_to={"rel_type": "m.thread", "event_id": "$other-root"},
                ),
                _message_event(
                    event_id="$indirect-agent-message",
                    sender="@agent:localhost",
                    body="Indirect answer",
                    origin_server_ts=3000,
                    relates_to=indirect_relation,
                ),
            ],
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is not None
    assert snapshot.content["body"] == expected_body


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_follows_reply_to_thread_child_edit(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """An edit remains an ancestry node even though it is not a visible snapshot candidate."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$thread-child",
                    sender="@user:localhost",
                    body="Detail",
                    origin_server_ts=1500,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
                _message_event(
                    event_id="$thread-child-edit",
                    sender="@user:localhost",
                    body="* Updated detail",
                    origin_server_ts=2000,
                    relates_to={"rel_type": "m.replace", "event_id": "$thread-child"},
                    new_content={"body": "Updated detail"},
                ),
                _message_event(
                    event_id="$agent-reply",
                    sender="@agent:localhost",
                    body="Answer",
                    origin_server_ts=2500,
                    relates_to={"m.in_reply_to": {"event_id": "$thread-child-edit"}},
                ),
            ],
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is not None
    assert snapshot.content["body"] == "Answer"


@pytest.mark.asyncio
async def test_state_thread_child_cannot_authorize_indirect_snapshot_member(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A poisoned state row cannot seed a thread graph for an indirect agent reply."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        state_child = _message_event(
            event_id="$state-child",
            sender="@user:localhost",
            body="Poisoned state child",
            origin_server_ts=1500,
            relates_to={"rel_type": "m.thread", "event_id": thread_id},
        )
        state_child["state_key"] = ""
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                state_child,
                _message_event(
                    event_id="$indirect-agent-message",
                    sender="@agent:localhost",
                    body="Forged membership",
                    origin_server_ts=2000,
                    relates_to={"m.in_reply_to": {"event_id": "$state-child"}},
                ),
            ],
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is None


@pytest.mark.asyncio
async def test_malformed_original_edit_cannot_authorize_indirect_snapshot_member(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A raw-source lookup must not re-admit a filtered malformed relation ancestor."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        ancestor = _message_event(
            event_id="$ancestor",
            sender="@user:localhost",
            body="Ancestor",
            origin_server_ts=2000,
            relates_to={"rel_type": "m.thread", "event_id": thread_id},
        )
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$thread-child",
                    sender="@user:localhost",
                    body="Valid child",
                    origin_server_ts=1500,
                    relates_to={"rel_type": "m.thread", "event_id": thread_id},
                ),
                ancestor,
                _message_event(
                    event_id="$ancestor-edit",
                    sender="@user:localhost",
                    body="* Ancestor",
                    origin_server_ts=2500,
                    relates_to={"rel_type": "m.replace", "event_id": "$ancestor"},
                    new_content={"body": "Edited ancestor"},
                ),
                _message_event(
                    event_id="$indirect-agent-message",
                    sender="@agent:localhost",
                    body="Forged membership",
                    origin_server_ts=3000,
                    relates_to={"m.in_reply_to": {"event_id": "$ancestor"}},
                ),
            ],
        )
        malformed_ancestor = {
            **ancestor,
            "room_id": room_id,
            "content": {
                "body": "missing msgtype",
                "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
            },
        }
        await _overwrite_cached_event_payload(
            cache,
            room_id=room_id,
            event_id="$ancestor",
            event=malformed_ancestor,
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_rejects_legacy_wrong_room_payload(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A legacy row's indexed room cannot override explicit foreign-room evidence."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        event_id = "$wrong-room"
        event = _message_event(
            event_id=event_id,
            sender="@agent:localhost",
            body="Forged",
            origin_server_ts=2000,
        )
        await cache.store_event(event_id, room_id, event)
        await _overwrite_cached_event_payload(
            cache,
            room_id=room_id,
            event_id=event_id,
            event={**event, "room_id": "!other:localhost"},
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            None,
            "@agent:localhost",
            runtime_started_at=0.0,
        )
    finally:
        await cache.close()

    assert snapshot is None


@pytest.mark.parametrize(
    "edit_sender",
    ["@user:localhost", "@attacker:localhost"],
    ids=["same-sender", "foreign-sender"],
)
@pytest.mark.asyncio
async def test_replacement_relation_cannot_authorize_indirect_snapshot_member(
    event_cache_factory: Callable[[], ConversationEventCache],
    edit_sender: str,
) -> None:
    """A replacement row cannot create membership independently of its original event."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        room_id = "!room:localhost"
        thread_id = "$thread-root"
        await _replace_thread(
            cache,
            room_id,
            thread_id,
            [
                _message_event(
                    event_id=thread_id,
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=1000,
                ),
                _message_event(
                    event_id="$root-edit",
                    sender=edit_sender,
                    body="* Question",
                    origin_server_ts=1500,
                    relates_to={"rel_type": "m.replace", "event_id": thread_id},
                    new_content={
                        "body": "Edited question",
                        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
                    },
                ),
                _message_event(
                    event_id="$indirect-agent-message",
                    sender="@agent:localhost",
                    body="Forged membership",
                    origin_server_ts=2000,
                    relates_to={"m.in_reply_to": {"event_id": "$root-edit"}},
                ),
            ],
        )
        snapshot = await cache.get_latest_agent_message_snapshot(
            room_id,
            thread_id,
            "@agent:localhost",
            runtime_started_at=None,
        )
    finally:
        await cache.close()

    assert snapshot is None


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_returns_streaming_status_for_threaded_message(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Edited threaded messages should surface the latest visible stream status."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await _replace_thread(
            cache,
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
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
        origin_server_ts=2000,
    )


@pytest.mark.asyncio
async def test_get_latest_agent_message_snapshot_ignores_foreign_sender_edits(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Snapshot edits must come from the same sender as the original message."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        await _replace_thread(
            cache,
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
                    new_content={"body": "Finished"},
                ),
                _message_event(
                    event_id="$forged-edit",
                    sender="@attacker:localhost",
                    body="* Working...",
                    origin_server_ts=4000,
                    relates_to={"rel_type": "m.replace", "event_id": "$reply"},
                    new_content={"body": "Forged"},
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
            "body": "Finished",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
        origin_server_ts=2000,
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
        origin_server_ts=2000,
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
        await _replace_thread(
            cache,
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
        await _replace_thread(
            cache,
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
        await _replace_thread(
            cache,
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
        await _replace_thread(
            cache,
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
async def test_thread_scope_uses_authoritative_event_timestamp_after_point_update(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A stale thread-index timestamp cannot keep an older event first."""
    cache = event_cache_factory()
    await cache.initialize()
    try:
        newest = _message_event(
            event_id="$newest",
            sender="@agent:localhost",
            body="Initially newest",
            origin_server_ts=4000,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        )
        older = _message_event(
            event_id="$older",
            sender="@agent:localhost",
            body="Actually newest",
            origin_server_ts=3000,
            relates_to={"rel_type": "m.thread", "event_id": "$thread-root"},
        )
        await _replace_thread(
            cache,
            "!room:localhost",
            "$thread-root",
            [
                _message_event(
                    event_id="$thread-root",
                    sender="@user:localhost",
                    body="Question",
                    origin_server_ts=500,
                ),
                newest,
                older,
            ],
            validated_at=1000.0,
        )
        await cache.store_event(
            "$newest",
            "!room:localhost",
            {
                **newest,
                "origin_server_ts": 1000,
            },
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
            "body": "Actually newest",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
        },
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
