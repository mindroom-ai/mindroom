"""Tests for the SQLite-backed Matrix thread event cache."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.api import RelationshipType

from mindroom.matrix.client import fetch_thread_history
from mindroom.matrix.event_cache import EventCache
from mindroom.matrix.room_cache import cached_room_get_event
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
    cache = EventCache(tmp_path / "event_cache.db")
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
    cache = EventCache(tmp_path / "event_cache.db")
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

        cached_event = await cache.get_event("$reply")
        missing_event = await cache.get_event("$missing")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cached_event["content"]["body"] == "Reply in thread"
    assert missing_event is None


@pytest.mark.asyncio
async def test_individual_event_cache_strips_runtime_timing_marker(tmp_path: Path) -> None:
    """Batch event caching should drop in-memory timing objects before serialization."""
    cache = EventCache(tmp_path / "event_cache.db")
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
        cached_event = await cache.get_event("$reply")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert "com.mindroom.dispatch_pipeline_timing" not in cached_event


@pytest.mark.asyncio
async def test_thread_cache_store_populates_individual_event_lookup(tmp_path: Path) -> None:
    """Thread cache writes should also populate the individual event table."""
    cache = EventCache(tmp_path / "event_cache.db")
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
        cached_event = await cache.get_event("$reply")
    finally:
        await cache.close()

    assert cached_event is not None
    assert cached_event["event_id"] == "$reply"
    assert cached_event["content"]["body"] == "Cached reply"


@pytest.mark.asyncio
async def test_thread_event_cache_strips_runtime_timing_marker(tmp_path: Path) -> None:
    """Thread cache writes should strip runtime-only timing markers before JSON storage."""
    cache = EventCache(tmp_path / "event_cache.db")
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
        cached_event = await cache.get_event("$reply")
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
    cache = EventCache(tmp_path / "event_cache.db")
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
async def test_redaction_removes_individual_event_cache_entry(tmp_path: Path) -> None:
    """Redactions should also remove individually cached events."""
    cache = EventCache(tmp_path / "event_cache.db")
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
        assert await cache.get_event("$reply") is not None
        redacted = await cache.redact_event("!room:localhost", "$reply", thread_id="$thread_root")
        cached_event = await cache.get_event("$reply")
    finally:
        await cache.close()

    assert redacted is True
    assert cached_event is None


@pytest.mark.asyncio
async def test_fetch_thread_history_cache_hit_avoids_full_fetch_calls(tmp_path: Path) -> None:
    """Cache hits should bypass the full root-plus-relations fetch path."""
    cache = EventCache(tmp_path / "event_cache.db")
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
    cache = EventCache(tmp_path / "event_cache.db")
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
    broken_cache = MagicMock(spec=EventCache)
    broken_cache.get_thread_events = AsyncMock(side_effect=RuntimeError("db broken"))
    broken_cache.invalidate_thread = AsyncMock()
    broken_cache.store_thread_events = AsyncMock()

    history = await fetch_thread_history(client, "!room:localhost", "$thread_root", event_cache=broken_cache)

    assert [message.event_id for message in history] == ["$thread_root", "$reply"]
    broken_cache.get_thread_events.assert_awaited_once_with("!room:localhost", "$thread_root")
    assert broken_cache.invalidate_thread.await_count >= 1
    broken_cache.store_thread_events.assert_awaited_once()
