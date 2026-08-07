"""Tests for the SQLite-backed Matrix thread event cache."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.matrix.cache.sqlite_event_cache as event_cache_module
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps, _ThreadIdLookup
from mindroom.event_journal import ConversationPage
from mindroom.matrix import event_normalization
from mindroom.matrix.cache import (
    ConversationEventCache,
    ThreadAppendOutcome,
    ThreadCacheGap,
    sqlite_event_cache_events,
    sqlite_event_cache_threads,
    thread_cache_rejection_reason,
)
from mindroom.matrix.cache.event_batching import group_lookup_events_by_room
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_reads import ThreadReadMode
from mindroom.matrix.client_thread_history import (
    BulkThreadRefreshStats,
)
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.conversation_reads import ConversationReader  # noqa: TC001
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
)
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import (
    agent_response_should_respond,
    bind_runtime_paths,
    create_mock_room,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.identity_helpers import entity_ids
from tests.threading_helpers import EmptyProjection

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from nio.api import RelationshipType


def _empty_conversation_reader() -> ConversationReader:
    """Return a reader for a harness that has no journal store behind it.

    Not the same thing as stubbing a reader that does: these harnesses build a
    resolver directly, so there is no projection to reach and a fake page is
    the honest analogue of the conversation-cache mock they already carry.
    """
    page = ConversationPage(messages=(), refresh_pending=(), next_cursor=None)
    return cast(
        "ConversationReader",
        SimpleNamespace(read=AsyncMock(return_value=page), read_strict=AsyncMock(return_value=page)),
    )


def _conversation_cache_for_thread_reads(
    tmp_path: Path,
    event_cache: ConversationEventCache,
    *,
    client: object,
) -> MatrixConversationCache:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=event_cache,
        event_cache_write_coordinator=None,
    )
    return MatrixConversationCache(logger=MagicMock(), runtime=runtime, store=EmptyProjection())


def _set_dispatch_thread_read_timeout(conversation_cache: MatrixConversationCache, seconds: float) -> None:
    runtime_paths = conversation_cache.runtime.runtime_paths
    conversation_cache.runtime.runtime_paths = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "MINDROOM_DISPATCH_THREAD_READ_TIMEOUT_SECONDS": str(seconds),
        },
    )


def _pending_thread_cache_update_wait_tasks() -> set[asyncio.Task[object]]:
    return {
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and task.get_coro().__qualname__.endswith("ThreadReadPolicy._wait_for_pending_thread_cache_updates")
    }


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
    cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    events: list[dict[str, object]],
) -> None:
    """Seed one authoritative cached thread snapshot for tests."""
    await _replace_thread(cache, room_id, thread_id, events)


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


def test_group_lookup_events_by_room_normalizes_and_preserves_order() -> None:
    """Lookup event batch grouping should be shared by durable cache backends."""
    grouped_events = group_lookup_events_by_room(
        [
            (
                "$a",
                "!alpha:localhost",
                {
                    "type": "m.room.message",
                    "content": {"body": "alpha first"},
                    "com.mindroom.dispatch_pipeline_timing": {"resolution_ms": 12},
                },
            ),
            (
                "$b",
                "!beta:localhost",
                {
                    "type": "m.room.message",
                    "event_id": "$already-present",
                    "content": {"body": "beta first"},
                },
            ),
            (
                "$c",
                "!alpha:localhost",
                {
                    "type": "m.room.message",
                    "content": {"body": "alpha second"},
                },
            ),
        ],
    )

    assert list(grouped_events) == ["!alpha:localhost", "!beta:localhost"]
    assert grouped_events == {
        "!alpha:localhost": [
            (
                "$a",
                {
                    "type": "m.room.message",
                    "content": {"body": "alpha first"},
                    "event_id": "$a",
                },
            ),
            (
                "$c",
                {
                    "type": "m.room.message",
                    "content": {"body": "alpha second"},
                    "event_id": "$c",
                },
            ),
        ],
        "!beta:localhost": [
            (
                "$b",
                {
                    "type": "m.room.message",
                    "event_id": "$already-present",
                    "content": {"body": "beta first"},
                },
            ),
        ],
    }


@pytest.mark.asyncio
async def test_dispatch_context_waits_for_strict_thread_history_after_degraded_snapshot(
    tmp_path: Path,
) -> None:
    """A proven thread must fall back to strict history before dispatch planning."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "primary": AgentConfig(display_name="Primary", rooms=["!room:localhost"]),
                "secondary": AgentConfig(display_name="Secondary", rooms=["!room:localhost"]),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    route_paths = runtime_paths_for(config)
    route_ids = entity_ids(config, route_paths)
    runtime = BotRuntimeState(
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=MagicMock(),
        event_cache_write_coordinator=None,
    )
    resolver = ConversationResolver(
        ConversationResolverDeps(
            runtime=runtime,
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="primary",
            matrix_id=route_ids["primary"],
            conversation_cache=MagicMock(),
            relations=make_relation_lookup(),
            conversation_reader=_empty_conversation_reader(),
        ),
    )
    degraded_history = thread_history_result(
        [],
        is_full_history=False,
        diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True},
    )
    strict_history = thread_history_result(
        [
            ResolvedVisibleMessage.synthetic(
                sender=route_ids["primary"].full_id,
                body="I can handle this.",
                event_id="$agent-reply",
                thread_id="$thread:localhost",
            ),
            ResolvedVisibleMessage.synthetic(
                sender="@requester:localhost",
                body="Please continue.",
                event_id="$user-follow-up",
                thread_id="$thread:localhost",
            ),
        ],
        is_full_history=True,
    )
    event_info = MagicMock(spec=EventInfo)

    with (
        patch.object(
            resolver,
            "_explicit_thread_id_for_event",
            AsyncMock(return_value=_ThreadIdLookup(thread_id="$thread:localhost", thread_history=degraded_history)),
        ),
        patch.object(
            resolver,
            "_read_thread_messages",
            AsyncMock(return_value=strict_history),
        ) as read_thread_messages,
    ):
        result = await resolver._resolve_thread_context(
            "!room:localhost",
            "$incoming:localhost",
            event_info,
            mode=ThreadReadMode.DISPATCH_SNAPSHOT,
            caller_label="dispatch_context",
        )

    assert result.is_thread is True
    assert result.thread_id == "$thread:localhost"
    assert result.thread_history == strict_history
    assert result.requires_model_history_refresh is False
    assert result.replay_guard_degraded is False
    read_thread_messages.assert_awaited_once_with(
        "!room:localhost",
        "$thread:localhost",
        mode=ThreadReadMode.STRICT_FULL,
        caller_label="dispatch_context_strict_thread_fallback",
    )
    assert agent_response_should_respond(
        agent_name="primary",
        am_i_mentioned=False,
        is_thread=True,
        room=create_mock_room("!room:localhost", ["primary", "secondary"], config),
        thread_history=result.thread_history,
        config=config,
        runtime_paths=route_paths,
        sender_id="@requester:localhost",
        available_responders_in_room=[route_ids["primary"], route_ids["secondary"]],
    )


@pytest.mark.asyncio
async def test_conversation_cache_startup_prewarm_bulk_refresh_preserves_metadata(
    tmp_path: Path,
) -> None:
    """Startup prewarm should call the bulk room refresher with fixed metadata."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)
    stats = BulkThreadRefreshStats(
        requested_threads=1,
        usable_threads=1,
        missing_root_ids=frozenset(),
        room_scan_pages=1,
        scanned_event_count=2,
    )
    bulk_refresh_room_thread_histories = AsyncMock(return_value=stats)

    try:
        with patch(
            "mindroom.matrix.conversation_cache.bulk_refresh_room_thread_histories",
            bulk_refresh_room_thread_histories,
        ):
            result = await conversation_cache._bulk_refresh_startup_threads(
                "!room:localhost",
                ["$thread:localhost"],
            )

        assert result == stats
        bulk_refresh_room_thread_histories.assert_awaited_once_with(
            client,
            "!room:localhost",
            event_cache,
            thread_root_ids=["$thread:localhost"],
            caller_label="startup_thread_prewarm",
            max_scan_pages=20,
        )
    finally:
        await event_cache.close()


@pytest.mark.asyncio
async def test_thread_snapshot_storage_exposes_direct_gap_reads(tmp_path: Path) -> None:
    """A stored snapshot should expose the newest gap marker recorded against its thread."""
    db, _maintenance_report, _generation = await event_cache_module._initialize_event_cache_db(
        tmp_path / "event_cache.db",
    )

    try:
        await sqlite_event_cache_threads.replace_thread_locked(
            db,
            principal_id="__mindroom_default_principal__",
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
            stored_at=100.0,
            fetch_started_at=100.0,
        )
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await sqlite_event_cache_threads.mark_thread_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="thread_stale",
            )
            await sqlite_event_cache_threads.mark_room_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="room_stale",
            )
        await db.commit()

        gap = await sqlite_event_cache_threads.load_thread_cache_gap(
            db,
            principal_id="__mindroom_default_principal__",
            room_id="!room:localhost",
            thread_id="$thread_root",
        )
    finally:
        await db.close()

    # One marker per thread, not a thread column joined against a room column: the room-scoped
    # marker fanned out onto this thread's row and, arriving no earlier, owns the reason.
    assert gap is not None
    assert gap.gap_marked_at == 200.0
    assert gap.gap_reason == "room_stale"
    assert thread_cache_rejection_reason(gap) == "room_stale"


@pytest.mark.asyncio
async def test_sqlite_gap_markers_are_monotonic(tmp_path: Path) -> None:
    """An older gap marker must not downgrade a newer one, at either scope."""
    db, _maintenance_report, _generation = await event_cache_module._initialize_event_cache_db(
        tmp_path / "event_cache.db",
    )

    try:
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await sqlite_event_cache_threads.mark_thread_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="newer_thread_marker",
            )
            await sqlite_event_cache_threads.mark_room_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="newer_room_marker",
            )
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=100.0):
            await sqlite_event_cache_threads.mark_thread_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="older_thread_marker",
            )
            await sqlite_event_cache_threads.mark_room_gap_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="older_room_marker",
            )
        await db.commit()

        gap = await sqlite_event_cache_threads.load_thread_cache_gap(
            db,
            principal_id="__mindroom_default_principal__",
            room_id="!room:localhost",
            thread_id="$thread_root",
        )
    finally:
        await db.close()

    assert gap is not None
    assert gap.gap_marked_at == 200.0
    assert gap.gap_reason == "newer_room_marker"


@pytest.mark.parametrize(
    ("gap", "expected_reason"),
    [
        pytest.param(None, None, id="no_marker_is_usable"),
        pytest.param(
            ThreadCacheGap(gap_marked_at=100.0, gap_reason="limited_sync_timeline"),
            "limited_sync_timeline",
            id="marker_reports_its_reason",
        ),
        pytest.param(
            ThreadCacheGap(gap_marked_at=100.0, gap_reason=None),
            "thread_gap_marked",
            id="reasonless_marker_still_rejects",
        ),
    ],
)
def test_thread_cache_rejection_reason_rule_table(
    gap: ThreadCacheGap | None,
    expected_reason: str | None,
) -> None:
    """The snapshot gate asks exactly one question: is a gap recorded against this thread."""
    assert thread_cache_rejection_reason(gap) == expected_reason


@pytest.mark.asyncio
async def test_thread_gap_marked_midflight_survives_the_replacement(tmp_path: Path) -> None:
    """A gap marked after a fetch began is not covered by that fetch, so it outlives it."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    root_source = {
        "event_id": "$thread_root",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": {"body": "Root message", "msgtype": "m.text"},
    }

    try:
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source], fetch_started_at=100.0)
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await cache.mark_thread_gap("!room:localhost", "$thread_root", reason="live_thread_mutation")

        # This fetch started before the marker, so it cannot have seen what the marker describes.
        stored_behind_marker = await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=150.0,
        )
        gap_after_uncovered_fetch = await cache.get_thread_cache_gap("!room:localhost", "$thread_root")

        # This one started after it, so it covers the marker and clears it.
        stored_after_marker = await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=250.0,
        )
        gap_after_covering_fetch = await cache.get_thread_cache_gap("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    # The snapshot installs either way -- refusing it would strand the thread -- but the marker is
    # what decides whether the next read may use it.
    assert stored_behind_marker
    assert gap_after_uncovered_fetch is not None
    assert gap_after_uncovered_fetch.gap_marked_at == 200.0
    assert thread_cache_rejection_reason(gap_after_uncovered_fetch) == "live_thread_mutation"

    assert stored_after_marker
    assert gap_after_covering_fetch is None


@pytest.mark.asyncio
async def test_room_gap_marked_midflight_survives_the_replacement(tmp_path: Path) -> None:
    """The room-scoped marker follows the same covering rule once it has fanned out."""
    cache = SqliteEventCache(tmp_path / "event_cache.db")
    await cache.initialize()
    root_source = {
        "event_id": "$thread_root",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": {"body": "Root message", "msgtype": "m.text"},
    }

    try:
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source], fetch_started_at=100.0)
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await cache.mark_room_threads_gap("!room:localhost", reason="sync_thread_lookup_unavailable")

        stored = await cache.replace_thread(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=150.0,
        )
        gap = await cache.get_thread_cache_gap("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert stored
    assert gap is not None
    assert gap.gap_marked_at == 200.0
    assert thread_cache_rejection_reason(gap) == "sync_thread_lookup_unavailable"


@pytest.mark.asyncio
async def test_event_cache_store_and_retrieve(event_cache: ConversationEventCache) -> None:
    """Stored events should round-trip in timestamp order."""
    cache = event_cache

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
async def test_get_recent_room_thread_ids_orders_by_latest_event_in_each_thread(
    event_cache: ConversationEventCache,
) -> None:
    """Recent thread IDs should be ordered by the freshest cached event per thread, not by root timestamp."""
    cache = event_cache

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
async def test_get_recent_room_events_warm_path(
    event_cache: ConversationEventCache,
) -> None:
    """Recent room event lookups should filter by room, type, timestamp, and limit."""
    cache = event_cache

    events = [
        (
            "$approval_old",
            "!room:localhost",
            {
                "event_id": "$approval_old",
                "sender": "@bot:localhost",
                "origin_server_ts": 1000,
                "type": "io.mindroom.tool_approval",
                "content": {"approval_id": "old"},
            },
        ),
        (
            "$approval_recent_1",
            "!room:localhost",
            {
                "event_id": "$approval_recent_1",
                "sender": "@bot:localhost",
                "origin_server_ts": 3000,
                "type": "io.mindroom.tool_approval",
                "content": {"approval_id": "recent-1"},
            },
        ),
        (
            "$message_newer",
            "!room:localhost",
            {
                "event_id": "$message_newer",
                "sender": "@user:localhost",
                "origin_server_ts": 5000,
                "type": "m.room.message",
                "content": {"body": "ignore", "msgtype": "m.text"},
            },
        ),
        (
            "$approval_other_room",
            "!other-room:localhost",
            {
                "event_id": "$approval_other_room",
                "sender": "@bot:localhost",
                "origin_server_ts": 6000,
                "type": "io.mindroom.tool_approval",
                "content": {"approval_id": "other-room"},
            },
        ),
        (
            "$approval_recent_2",
            "!room:localhost",
            {
                "event_id": "$approval_recent_2",
                "sender": "@bot:localhost",
                "origin_server_ts": 7000,
                "type": "io.mindroom.tool_approval",
                "content": {"approval_id": "recent-2"},
            },
        ),
    ]

    try:
        await cache.store_events_batch(events)
        all_recent = await cache.get_recent_room_events(
            "!room:localhost",
            event_type="io.mindroom.tool_approval",
            since_ts_ms=2000,
        )
        first_only = await cache.get_recent_room_events(
            "!room:localhost",
            event_type="io.mindroom.tool_approval",
            since_ts_ms=2000,
            limit=1,
        )
    finally:
        await cache.close()

    assert [event["event_id"] for event in all_recent] == ["$approval_recent_2", "$approval_recent_1"]
    assert [event["event_id"] for event in first_only] == ["$approval_recent_2"]


@pytest.mark.asyncio
async def test_event_cache_preserves_insertion_order_for_same_timestamp_events(
    event_cache: ConversationEventCache,
) -> None:
    """Cached reads should preserve the stored order when timestamps tie."""
    cache = event_cache

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
async def test_individual_event_cache_store_and_retrieve(event_cache: ConversationEventCache) -> None:
    """Individually cached events should round-trip by event ID."""
    cache = event_cache

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


def _clear_payload(
    event_id: str,
    *,
    body: str = "clear",
    thread_root_id: str | None = None,
    edit_of: str | None = None,
    origin_server_ts: int = 1000,
) -> dict[str, object]:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    if edit_of is not None:
        content["body"] = f"* {body}"
        content["m.new_content"] = {"body": body, "msgtype": "m.text"}
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": edit_of}
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": origin_server_ts,
        "type": "m.room.message",
        "content": content,
    }


def _opaque_payload(
    event_id: str,
    *,
    thread_root_id: str | None = None,
    origin_server_ts: int = 1000,
) -> dict[str, object]:
    content: dict[str, object] = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "opaque ciphertext",
        "device_id": "DEVICE",
        "sender_key": "sender-key",
        "session_id": "session",
    }
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": origin_server_ts,
        "type": "m.room.encrypted",
        "content": content,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival_order", [("clear", "opaque"), ("opaque", "clear")])
async def test_point_payload_upgrade_is_monotonic_across_arrival_orders(
    event_cache: ConversationEventCache,
    arrival_order: tuple[str, str],
) -> None:
    """Opaque ciphertext must never replace a decrypted point payload in either arrival order.

    The divergent thread roots make index derivation observable: a refused payload must
    contribute no thread index rows, so the index always describes the accepted payload.
    """
    room_id = "!room:localhost"
    event_id = "$mixed:localhost"
    payloads = {
        "clear": _clear_payload(event_id, body="decrypted", thread_root_id="$clear-root:localhost"),
        "opaque": _opaque_payload(event_id, thread_root_id="$opaque-root:localhost"),
    }

    for payload_kind in arrival_order:
        await event_cache.store_event(event_id, room_id, payloads[payload_kind])

    cached_event = await event_cache.get_event(room_id, event_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.message"
    assert cached_event["content"]["body"] == "decrypted"
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == "$clear-root:localhost"
    assert await event_cache.get_thread_id_for_event(room_id, "$clear-root:localhost") == "$clear-root:localhost"
    if arrival_order == ("clear", "opaque"):
        assert await event_cache.get_thread_id_for_event(room_id, "$opaque-root:localhost") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch_order",
    [
        ("clear", "opaque"),
        ("opaque", "clear"),
        ("opaque", "clear", "opaque"),
    ],
)
async def test_duplicate_ids_in_one_batch_converge_on_clear_payload(
    event_cache: ConversationEventCache,
    batch_order: tuple[str, ...],
) -> None:
    """Duplicate event IDs inside one batch must converge on the decrypted payload."""
    room_id = "!room:localhost"
    event_id = "$duplicated:localhost"
    thread_root_id = "$root:localhost"
    payloads = {
        "clear": _clear_payload(event_id, body="decrypted", thread_root_id=thread_root_id),
        "opaque": _opaque_payload(event_id, thread_root_id=thread_root_id),
    }

    await event_cache.store_events_batch(
        [(event_id, room_id, payloads[payload_kind]) for payload_kind in batch_order],
    )

    cached_event = await event_cache.get_event(room_id, event_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.message"
    assert cached_event["content"]["body"] == "decrypted"
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == thread_root_id
    assert await event_cache.get_thread_id_for_event(room_id, thread_root_id) == thread_root_id


@pytest.mark.asyncio
async def test_chained_thread_relations_keep_the_middle_event_as_its_own_root(
    event_cache: ConversationEventCache,
) -> None:
    """An event that is both a thread child and another event's root maps to itself.

    The derived index rows repeat that middle event ID under two different thread IDs: once as a
    child of the outer root, and once as the self row every learned root gets. A batched upsert has
    to collapse the repeat to the row the sequential write left behind, because
    ``ON CONFLICT DO UPDATE`` cannot touch the same row twice in one statement.
    """
    room_id = "!room:localhost"
    child_id = "$child:localhost"
    middle_id = "$middle:localhost"
    outer_id = "$outer:localhost"

    await event_cache.store_events_batch(
        [
            (child_id, room_id, _clear_payload(child_id, body="child", thread_root_id=middle_id)),
            (middle_id, room_id, _clear_payload(middle_id, body="middle", thread_root_id=outer_id)),
        ],
    )

    assert await event_cache.get_thread_id_for_event(room_id, child_id) == middle_id
    assert await event_cache.get_thread_id_for_event(room_id, middle_id) == middle_id
    assert await event_cache.get_thread_id_for_event(room_id, outer_id) == outer_id


@pytest.mark.asyncio
async def test_repeated_edit_ids_in_one_batch_keep_the_last_edit_index_row(
    event_cache: ConversationEventCache,
) -> None:
    """A batch naming one edit event twice indexes that edit once, keeping the last payload.

    Both occurrences are accepted -- clear content never loses to clear content -- so the derived
    edit-index rows repeat the same ``edit_event_id``, which a batched upsert has to collapse.
    """
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"

    await event_cache.store_events_batch(
        [(original_id, room_id, _clear_payload(original_id, body="original"))],
    )
    await event_cache.store_events_batch(
        [
            (edit_id, room_id, _clear_payload(edit_id, body="first edit", edit_of=original_id)),
            (edit_id, room_id, _clear_payload(edit_id, body="second edit", edit_of=original_id)),
        ],
    )

    latest_edit = await event_cache.get_latest_edit(room_id, original_id)
    assert latest_edit is not None
    assert latest_edit["event_id"] == edit_id
    assert latest_edit["content"]["m.new_content"]["body"] == "second edit"


@pytest.mark.asyncio
async def test_one_write_settles_proven_and_unproven_thread_roots_together(
    event_cache: ConversationEventCache,
) -> None:
    """Re-parenting the only child proves the new root and unproves the old one in one write."""
    room_id = "!room:localhost"
    child_id = "$child:localhost"
    old_root_id = "$old-root:localhost"
    new_root_id = "$new-root:localhost"

    await event_cache.store_events_batch(
        [(child_id, room_id, _clear_payload(child_id, body="first", thread_root_id=old_root_id))],
    )
    assert await event_cache.get_thread_id_for_event(room_id, old_root_id) == old_root_id

    await event_cache.store_events_batch(
        [(child_id, room_id, _clear_payload(child_id, body="reparented", thread_root_id=new_root_id))],
    )

    assert await event_cache.get_thread_id_for_event(room_id, child_id) == new_root_id
    assert await event_cache.get_thread_id_for_event(room_id, new_root_id) == new_root_id
    assert await event_cache.get_thread_id_for_event(room_id, old_root_id) is None


@pytest.mark.asyncio
async def test_repeated_event_in_a_snapshot_keeps_its_last_position_on_every_backend(
    event_cache: ConversationEventCache,
) -> None:
    """A snapshot of ``A, B, A-last`` reads back as ``B, A`` on both backends.

    Membership rows are ordered by ``origin_server_ts`` and then by the sequence value each write
    draws, so when the timestamps tie the write order decides the read order. The sequential loop
    rewrote ``A`` after ``B``, leaving ``A`` newer. A batched upsert that collapsed the repeat to
    ``A``'s *first* position would draw ``A``'s sequence value before ``B``'s and silently reverse
    the pair against SQLite.
    """
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    first_id = "$a:localhost"
    second_id = "$b:localhost"
    tied_ts = 1000

    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [
            _clear_payload(thread_id, body="root", origin_server_ts=tied_ts),
            _clear_payload(first_id, body="a-first", thread_root_id=thread_id, origin_server_ts=tied_ts),
            _clear_payload(second_id, body="b", thread_root_id=thread_id, origin_server_ts=tied_ts),
            _clear_payload(first_id, body="a-last", thread_root_id=thread_id, origin_server_ts=tied_ts),
        ],
    )

    thread_events = await event_cache.get_thread_events(room_id, thread_id)
    assert thread_events is not None
    assert [event["event_id"] for event in thread_events] == [thread_id, second_id, first_id]
    assert thread_events[-1]["content"]["body"] == "a-last"


@pytest.mark.asyncio
async def test_repeated_event_in_one_thread_snapshot_binds_the_thread_once(
    event_cache: ConversationEventCache,
) -> None:
    """A snapshot naming one event twice binds it to the thread exactly once."""
    room_id = "!room:localhost"
    thread_id = "$thread-root:localhost"
    duplicated_id = "$duplicated:localhost"
    root_source = _clear_payload(thread_id, body="root", origin_server_ts=1000)
    reply = _clear_payload(duplicated_id, body="reply", thread_root_id=thread_id, origin_server_ts=1100)

    await _replace_thread(event_cache, room_id, thread_id, [root_source, reply, reply])

    thread_events = await event_cache.get_thread_events(room_id, thread_id)
    assert thread_events is not None
    assert [event["event_id"] for event in thread_events] == [thread_id, duplicated_id]
    assert await event_cache.get_thread_id_for_event(room_id, duplicated_id) == thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival_order", [("clear", "opaque"), ("opaque", "clear")])
async def test_separate_cache_clients_cannot_downgrade_decrypted_payload(
    event_cache_factory: Callable[[], ConversationEventCache],
    arrival_order: tuple[str, str],
) -> None:
    """Two cache clients on one backing store must converge on the decrypted payload."""
    room_id = "!room:localhost"
    event_id = "$shared:localhost"
    thread_root_id = "$root:localhost"
    decrypting_client = event_cache_factory()
    await decrypting_client.initialize()
    try:
        keyless_client = event_cache_factory()
        await keyless_client.initialize()
        try:
            writers = {"clear": decrypting_client, "opaque": keyless_client}
            payloads = {
                "clear": _clear_payload(event_id, body="decrypted", thread_root_id=thread_root_id),
                "opaque": _opaque_payload(event_id, thread_root_id=thread_root_id),
            }
            for payload_kind in arrival_order:
                await writers[payload_kind].store_event(event_id, room_id, payloads[payload_kind])
            cached_by_decrypting = await decrypting_client.get_event(room_id, event_id)
            cached_by_keyless = await keyless_client.get_event(room_id, event_id)
        finally:
            await keyless_client.close()
    finally:
        await decrypting_client.close()

    for cached_event in (cached_by_decrypting, cached_by_keyless):
        assert cached_event is not None
        assert cached_event["type"] == "m.room.message"
        assert cached_event["content"]["body"] == "decrypted"


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival_order", [("clear", "opaque"), ("opaque", "clear")])
async def test_thread_append_preserves_decrypted_payload_across_arrival_orders(
    event_cache: ConversationEventCache,
    arrival_order: tuple[str, str],
) -> None:
    """Incremental appends must never downgrade an already-decrypted thread snapshot row."""
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    child_event_id = "$child:localhost"
    await _replace_thread(event_cache, room_id, thread_id, [_clear_payload(thread_id, body="root")])
    payloads = {
        "clear": _clear_payload(
            child_event_id,
            body="decrypted child",
            thread_root_id=thread_id,
            origin_server_ts=2000,
        ),
        "opaque": _opaque_payload(child_event_id, thread_root_id=thread_id, origin_server_ts=2000),
    }

    for payload_kind in arrival_order:
        outcome = await event_cache.apply_thread_mutation_append(
            room_id,
            thread_id,
            payloads[payload_kind],
            append_failed_reason="live_append_failed",
        )
        assert outcome is ThreadAppendOutcome.APPENDED

    thread_events = await event_cache.get_thread_events(room_id, thread_id)
    assert thread_events is not None
    cached_child = next(event for event in thread_events if event["event_id"] == child_event_id)
    assert cached_child["type"] == "m.room.message"
    assert cached_child["content"]["body"] == "decrypted child"
    cached_event = await event_cache.get_event(room_id, child_event_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.message"
    assert cached_event["content"]["body"] == "decrypted child"


@pytest.mark.asyncio
async def test_thread_replacement_preserves_decrypted_payload(
    event_cache: ConversationEventCache,
) -> None:
    """A full snapshot replacement must not bypass the clear-payload invariant."""
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [_clear_payload(thread_id, body="decrypted root")],
    )

    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [_opaque_payload(thread_id)],
    )

    cached_event = await event_cache.get_event(room_id, thread_id)
    thread_events = await event_cache.get_thread_events(room_id, thread_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.message"
    assert cached_event["content"]["body"] == "decrypted root"
    assert thread_events is not None
    assert len(thread_events) == 1
    assert thread_events[0]["type"] == "m.room.message"
    assert thread_events[0]["content"]["body"] == "decrypted root"


@pytest.mark.asyncio
async def test_refused_opaque_thread_replacement_preserves_mxc_plaintext(
    event_cache: ConversationEventCache,
) -> None:
    """A refused ciphertext snapshot must retain the clear payload's sidecar ownership."""
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    mxc_url = "mxc://server/decrypted-sidecar"
    clear_event = _clear_payload(thread_id, body="decrypted root")
    clear_event["content"] = {
        "body": "preview",
        "msgtype": "m.file",
        "url": mxc_url,
        "io.mindroom.long_text": {
            "version": 2,
            "encoding": "matrix_event_content_json",
        },
    }
    await _replace_thread(event_cache, room_id, thread_id, [clear_event])
    assert await event_cache.store_mxc_text(room_id, thread_id, mxc_url, "decrypted sidecar")

    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [_opaque_payload(thread_id)],
    )

    assert await event_cache.get_mxc_text(room_id, thread_id, mxc_url) == "decrypted sidecar"


@pytest.mark.asyncio
async def test_refused_opaque_snapshot_still_records_explicit_thread_membership(
    event_cache: ConversationEventCache,
) -> None:
    """Snapshot membership must be indexed even when its opaque payload is refused."""
    room_id = "!room:localhost"
    thread_id = "$root:localhost"
    await event_cache.store_event(
        thread_id,
        room_id,
        _clear_payload(thread_id, body="decrypted root"),
    )
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) is None

    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [_opaque_payload(thread_id)],
    )

    cached_event = await event_cache.get_event(room_id, thread_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.message"
    assert cached_event["content"]["body"] == "decrypted root"
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) == thread_id


@pytest.mark.asyncio
async def test_refused_opaque_write_keeps_latest_edit_join_readable(
    event_cache: ConversationEventCache,
) -> None:
    """A keyless client's ciphertext for an indexed edit must not corrupt latest-edit reads."""
    room_id = "!room:localhost"
    original_event_id = "$original:localhost"
    edit_event_id = "$edit:localhost"
    await event_cache.store_event(
        original_event_id,
        room_id,
        _clear_payload(original_event_id, body="original"),
    )
    await event_cache.store_event(
        edit_event_id,
        room_id,
        _clear_payload(edit_event_id, body="edited", edit_of=original_event_id, origin_server_ts=2000),
    )

    await event_cache.store_event(edit_event_id, room_id, _opaque_payload(edit_event_id, origin_server_ts=2000))

    latest_edit = await event_cache.get_latest_edit(room_id, original_event_id)
    assert latest_edit is not None
    assert latest_edit["type"] == "m.room.message"
    assert latest_edit["content"]["m.new_content"]["body"] == "edited"


@pytest.mark.asyncio
async def test_redaction_tombstone_survives_clear_and_opaque_rewrites(
    event_cache: ConversationEventCache,
) -> None:
    """The monotonic upsert must not resurrect durably redacted events for any payload quality."""
    room_id = "!room:localhost"
    event_id = "$redacted:localhost"
    await event_cache.store_event(event_id, room_id, _clear_payload(event_id))
    assert await event_cache.redact_event(room_id, event_id)

    await event_cache.store_event(event_id, room_id, _opaque_payload(event_id))
    await event_cache.store_event(event_id, room_id, _clear_payload(event_id))

    assert await event_cache.get_event(room_id, event_id) is None


@pytest.mark.asyncio
async def test_accepted_clear_rewrite_still_moves_thread_index_row(
    event_cache: ConversationEventCache,
) -> None:
    """Accepted clear rewrites must keep last-wins thread index moves working."""
    room_id = "!room:localhost"
    event_id = "$moved:localhost"
    await event_cache.store_events_batch(
        [(event_id, room_id, _clear_payload(event_id, thread_root_id="$root-a:localhost"))],
    )
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == "$root-a:localhost"

    await event_cache.store_events_batch(
        [(event_id, room_id, _clear_payload(event_id, thread_root_id="$root-b:localhost"))],
    )

    assert await event_cache.get_thread_id_for_event(room_id, event_id) == "$root-b:localhost"


@pytest.mark.asyncio
async def test_opaque_payload_remains_retained_and_refreshable(
    event_cache: ConversationEventCache,
) -> None:
    """Opaque events must stay retained and refreshable until clear content improves them."""
    room_id = "!room:localhost"
    event_id = "$opaque-only:localhost"
    thread_root_id = "$root:localhost"
    await event_cache.store_event(event_id, room_id, _opaque_payload(event_id, thread_root_id=thread_root_id))

    cached_event = await event_cache.get_event(room_id, event_id)
    assert cached_event is not None
    assert cached_event["type"] == "m.room.encrypted"
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == thread_root_id
    assert await event_cache.get_thread_id_for_event(room_id, thread_root_id) == thread_root_id

    await event_cache.store_event(event_id, room_id, _opaque_payload(event_id, thread_root_id=thread_root_id))

    refreshed_event = await event_cache.get_event(room_id, event_id)
    assert refreshed_event is not None
    assert refreshed_event["type"] == "m.room.encrypted"


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
        principal_id: str,
        room_id: str,
        event_id: str,
    ) -> dict[str, object] | None:
        operation_started.set()
        await allow_operation_finish.wait()
        return await original_load_event(
            db,
            principal_id=principal_id,
            room_id=room_id,
            event_id=event_id,
        )

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
async def test_individual_event_cache_strips_runtime_timing_marker(event_cache: ConversationEventCache) -> None:
    """Batch event caching should drop in-memory timing objects before serialization."""
    cache = event_cache

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
async def test_thread_cache_store_populates_individual_event_lookup(event_cache: ConversationEventCache) -> None:
    """Thread cache writes should also populate the individual event table."""
    cache = event_cache

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
async def test_thread_event_cache_strips_runtime_timing_marker(event_cache: ConversationEventCache) -> None:
    """Thread cache writes should strip runtime-only timing markers before JSON storage."""
    cache = event_cache

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
async def test_edit_cache_row_indexes_io_mindroom_tool_approval_edits(
    event_cache: ConversationEventCache,
) -> None:
    """Custom approval-card edits must be visible through the latest-edit index."""
    cache = event_cache

    approval_card = {
        "event_id": "$approval",
        "sender": "@bot:localhost",
        "origin_server_ts": 1000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "approval_id": "approval-1",
            "requester_id": "@user:localhost",
            "status": "pending",
            "tool_name": "read_file",
        },
    }
    approval_edit = {
        "event_id": "$approval_edit",
        "sender": "@bot:localhost",
        "origin_server_ts": 2000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "approval_id": "approval-1",
            "requester_id": "@user:localhost",
            "status": "approved",
            "tool_name": "read_file",
            "m.new_content": {
                "approval_id": "approval-1",
                "requester_id": "@user:localhost",
                "status": "approved",
                "tool_name": "read_file",
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }

    try:
        await cache.store_events_batch(
            [
                ("$approval", "!room:localhost", approval_card),
                ("$approval_edit", "!room:localhost", approval_edit),
            ],
        )
        latest_edit = await cache.get_latest_edit("!room:localhost", "$approval")
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$approval_edit"
    assert latest_edit["content"]["m.new_content"]["status"] == "approved"


@pytest.mark.asyncio
async def test_latest_edit_can_be_scoped_to_sender_when_newer_edit_is_untrusted(
    event_cache: ConversationEventCache,
) -> None:
    """Approval lookup should be able to ignore newer edits from other senders."""
    cache = event_cache

    approval_card = {
        "event_id": "$approval",
        "sender": "@bot:localhost",
        "origin_server_ts": 1000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "approval_id": "approval-1",
            "requester_id": "@user:localhost",
            "status": "pending",
            "tool_name": "read_file",
        },
    }
    trusted_edit = {
        "event_id": "$trusted_edit",
        "sender": "@bot:localhost",
        "origin_server_ts": 2000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "approval_id": "approval-1",
            "requester_id": "@user:localhost",
            "status": "approved",
            "tool_name": "read_file",
            "m.new_content": {
                "approval_id": "approval-1",
                "requester_id": "@user:localhost",
                "status": "approved",
                "tool_name": "read_file",
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }
    untrusted_edit = {
        "event_id": "$untrusted_edit",
        "sender": "@attacker:localhost",
        "origin_server_ts": 3000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "approval_id": "approval-1",
            "requester_id": "@user:localhost",
            "status": "denied",
            "tool_name": "read_file",
            "m.new_content": {
                "approval_id": "approval-1",
                "requester_id": "@user:localhost",
                "status": "denied",
                "tool_name": "read_file",
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }

    try:
        await cache.store_events_batch(
            [
                ("$approval", "!room:localhost", approval_card),
                ("$trusted_edit", "!room:localhost", trusted_edit),
                ("$untrusted_edit", "!room:localhost", untrusted_edit),
            ],
        )
        latest_edit = await cache.get_latest_edit("!room:localhost", "$approval")
        latest_trusted_edit = await cache.get_latest_edit("!room:localhost", "$approval", sender="@bot:localhost")
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$untrusted_edit"
    assert latest_trusted_edit is not None
    assert latest_trusted_edit["event_id"] == "$trusted_edit"


@pytest.mark.asyncio
async def test_redaction_removes_individual_event_cache_entry(event_cache: ConversationEventCache) -> None:
    """Redactions should also remove individually cached events."""
    cache = event_cache

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
async def test_invalidate_thread_preserves_separately_cached_latest_edit(
    event_cache: ConversationEventCache,
) -> None:
    """Thread invalidation should not sever edit projection for separately cached edits."""
    cache = event_cache

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
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$reply_edit"


@pytest.mark.asyncio
async def test_invalidate_thread_removes_event_thread_rows(event_cache: ConversationEventCache) -> None:
    """Thread invalidation must also clear durable event-to-thread mappings."""
    cache = event_cache

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
async def test_store_events_batch_records_thread_root_self_mapping_from_explicit_thread_child(
    event_cache: ConversationEventCache,
) -> None:
    """Explicit threaded children should also make the root resolve to its own thread id."""
    cache = event_cache

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
async def test_store_events_batch_rolls_back_on_index_derivation_failure(
    event_cache: ConversationEventCache,
) -> None:
    """Failed batch writes must not leak partial point-lookup rows into later commits."""
    cache = event_cache

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
    assert schema_version == event_cache_module._EVENT_CACHE_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_mxc_text_cache_round_trips_across_event_cache_reopen(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Durable MXC text rows should survive closing and reopening the event cache."""
    cache = event_cache_factory()
    await cache.initialize()
    owner_event = {
        "event_id": "$sidecar-owner",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "sender": "@agent:localhost",
        "content": {
            "body": "preview",
            "msgtype": "m.file",
            "url": "mxc://server/sidecar",
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
        },
    }

    try:
        await cache.store_event("$sidecar-owner", "!room:localhost", owner_event)
        assert await cache.store_mxc_text(
            "!room:localhost",
            "$sidecar-owner",
            "mxc://server/sidecar",
            "Full text sidecar",
        )
    finally:
        await cache.close()

    reopened_cache = event_cache_factory()
    await reopened_cache.initialize()
    try:
        cached_text = await reopened_cache.get_mxc_text(
            "!room:localhost",
            "$sidecar-owner",
            "mxc://server/sidecar",
        )
    finally:
        await reopened_cache.close()

    assert cached_text == "Full text sidecar"
