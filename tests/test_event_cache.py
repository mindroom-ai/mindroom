"""Tests for the SQLite-backed Matrix thread event cache."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio.api import RelationshipType

import mindroom.matrix.cache.sqlite_event_cache as event_cache_module
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps, _ThreadIdLookup
from mindroom.matrix.cache import (
    ConversationEventCache,
    ThreadCacheReplaceOutcome,
    ThreadCacheState,
    event_normalization,
    sqlite_event_cache_events,
    sqlite_event_cache_threads,
    thread_cache_rejection_reason,
)
from mindroom.matrix.cache.event_batching import group_lookup_events_by_room
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.cache.thread_reads import ThreadReadMode
from mindroom.matrix.cache.thread_repair import (
    ThreadRepairBackoffError,
    ThreadRepairRegistry,
)
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.client_thread_history import (
    BulkThreadRefreshStats,
    RetainedThreadEventSourceProvider,
    fetch_thread_history,
)
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage, thread_root_body_preview
from mindroom.matrix.conversation_cache import MatrixConversationCache, _cached_room_get_event
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import valid_room_message_replacement
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    is_thread_history_degraded,
)
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadResolutionState,
    ThreadRootProof,
    conversation_relation_thread_membership_access,
    resolve_event_thread_membership,
)
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import (
    agent_response_should_respond,
    bind_runtime_paths,
    create_mock_room,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.event_cache_test_support import get_latest_edit
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from pathlib import Path

    from mindroom.matrix.cache import ThreadHistoryResult


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
    return MatrixConversationCache(logger=MagicMock(), runtime=runtime)


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
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": server_timestamp,
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
                    "event_id": "$b",
                    "content": {"body": "beta first"},
                },
            ),
        ],
    }


@pytest.mark.asyncio
async def test_conversation_cache_thread_reads_forward_client_fetch_metadata(
    tmp_path: Path,
) -> None:
    """Thread read modes should preserve the facade metadata passed to client fetchers."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)
    read_modes = [
        ("get_thread_history", "fetch_thread_history", True, 50.0),
        ("get_dispatch_thread_snapshot", "fetch_dispatch_thread_snapshot", False, 75.0),
        ("get_dispatch_thread_history", "fetch_dispatch_thread_history", True, 100.0),
    ]
    fetchers = {
        name: AsyncMock(return_value=thread_history_result([], is_full_history=is_full_history))
        for _method_name, name, is_full_history, _queue_wait_ms in read_modes
    }

    try:
        with (
            patch("mindroom.matrix.conversation_cache.fetch_thread_history", fetchers["fetch_thread_history"]),
            patch(
                "mindroom.matrix.conversation_cache.fetch_dispatch_thread_snapshot",
                fetchers["fetch_dispatch_thread_snapshot"],
            ),
            patch(
                "mindroom.matrix.conversation_cache.fetch_dispatch_thread_history",
                fetchers["fetch_dispatch_thread_history"],
            ),
            patch(
                "mindroom.matrix.cache.thread_reads.time.perf_counter",
                side_effect=[1.0, 1.05, 2.0, 2.01, 2.075, 2.075, 2.075, 3.0, 3.01, 3.1, 3.1, 3.1],
            ),
        ):
            read_methods = {
                "get_thread_history": conversation_cache.get_thread_history,
                "get_dispatch_thread_snapshot": conversation_cache.get_dispatch_thread_snapshot,
                "get_dispatch_thread_history": conversation_cache.get_dispatch_thread_history,
            }
            for method_name, _name, is_full_history, _queue_wait_ms in read_modes:
                result = await read_methods[method_name](
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label=f"caller-{method_name}",
                )
                assert result.is_full_history is is_full_history

        for method_name, name, _is_full_history, queue_wait_ms in read_modes:
            fetchers[name].assert_awaited_once_with(
                client,
                "!room:localhost",
                "$thread:localhost",
                event_cache=event_cache,
                trusted_sender_ids=conversation_cache._trusted_sender_ids(),
                caller_label=f"caller-{method_name}",
                coordinator_queue_wait_ms=queue_wait_ms,
                resolution_reuse=conversation_cache._thread_resolution_reuse,
                refill=None,
            )
    finally:
        await event_cache.close()


@pytest.mark.asyncio
async def test_conversation_cache_rejects_and_does_not_persist_mismatched_point_lookup(
    tmp_path: Path,
    event_cache: ConversationEventCache,
) -> None:
    """A successful lookup for another event ID must not poison either durable cache key."""
    room_id = "!room:localhost"
    requested_event_id = "$requested:localhost"
    returned_event_id = "$different:localhost"
    client = MagicMock()
    client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "content": {
                    "body": "forged",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "event_id": "$forged-root:localhost",
                        "rel_type": "m.thread",
                    },
                },
                "event_id": returned_event_id,
                "origin_server_ts": 1,
                "room_id": room_id,
                "sender": "@attacker:localhost",
                "type": "m.room.message",
            },
        ),
    )
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    response = await conversation_cache.get_event(room_id, requested_event_id)

    assert isinstance(response, nio.RoomGetEventError)
    assert await event_cache.get_event(room_id, requested_event_id) is None
    assert await event_cache.get_event(room_id, returned_event_id) is None


@pytest.mark.asyncio
async def test_conversation_cache_rejects_explicit_wrong_room_point_lookup(
    tmp_path: Path,
    event_cache: ConversationEventCache,
) -> None:
    """A point response with contradictory room evidence must fail closed."""
    room_id = "!room:localhost"
    event_id = "$requested:localhost"
    event = _make_text_event(
        event_id=event_id,
        sender="@attacker:localhost",
        body="Wrong room",
        server_timestamp=1000,
        source_content={"body": "Wrong room", "msgtype": "m.text"},
    )
    event.source["room_id"] = "!other:localhost"
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(event))
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    response = await conversation_cache.get_event(room_id, event_id)

    assert isinstance(response, nio.RoomGetEventError)
    assert await event_cache.get_event(room_id, event_id) is None


@pytest.mark.asyncio
async def test_dispatch_thread_read_degrades_when_cache_coordinator_never_drains(
    tmp_path: Path,
) -> None:
    """Dispatch-safe reads should not wait unbounded for advisory cache coordination."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    async def never_idle(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(side_effect=never_idle)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    _set_dispatch_thread_read_timeout(conversation_cache, 0.01)

    try:
        result = await asyncio.wait_for(
            conversation_cache.get_dispatch_thread_snapshot(
                "!room:localhost",
                "$thread:localhost",
                caller_label="dispatch_context",
            ),
            timeout=0.2,
        )
    finally:
        await event_cache.close()

    assert result == []
    assert result.is_full_history is False
    assert result.diagnostics["thread_read_degraded"] is True
    assert result.diagnostics["thread_read_error"] == "cache_coordinator_timeout"
    assert result.diagnostics["thread_read_source"] == "degraded"
    assert result.diagnostics["caller_label"] == "dispatch_context"
    coordinator.wait_for_thread_idle.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_thread_read_timeout_does_not_cancel_pending_cache_write(
    tmp_path: Path,
) -> None:
    """Timeouts around dispatch-safe coordinator waits must not cancel cache mutation tasks."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    release_write = asyncio.Event()
    write_started = asyncio.Event()

    async def pending_cache_write() -> None:
        write_started.set()
        await release_write.wait()

    pending_write_task = coordinator.queue_thread_update(
        "!room:localhost",
        "$thread:localhost",
        pending_cache_write,
        name="matrix_cache_pending_test_write",
        coordination_scope=event_cache.principal_id,
    )
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    _set_dispatch_thread_read_timeout(conversation_cache, 0.01)
    baseline_wait_tasks = _pending_thread_cache_update_wait_tasks()

    try:
        await asyncio.wait_for(write_started.wait(), timeout=0.2)
        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_snapshot",
            AsyncMock(side_effect=AssertionError("coordinator timeout should not fetch")),
        ):
            result = await asyncio.wait_for(
                conversation_cache.get_dispatch_thread_snapshot(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="dispatch_context",
                ),
                timeout=0.2,
            )

        assert result.diagnostics["thread_read_error"] == "cache_coordinator_timeout"
        assert pending_write_task.cancelled() is False
        assert pending_write_task.done() is False
        await asyncio.sleep(0)
        assert _pending_thread_cache_update_wait_tasks() == baseline_wait_tasks
    finally:
        release_write.set()
        await pending_write_task
        await event_cache.close()


@pytest.mark.asyncio
async def test_dispatch_thread_read_enters_repair_ownership_once_after_idle_wait(
    tmp_path: Path,
) -> None:
    """Dispatch fetches should claim repair ownership exactly once, after the bounded idle wait.

    Joining an existing flight is owned by the registry and covered in ``test_thread_repair``; this
    read-path test stubs the coordinator, so it can only observe that ownership is claimed once.
    """
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.pending_thread_repair_deltas.return_value = ()

    async def run_thread_repair(
        _room_id: str,
        _thread_id: str,
        repair: Callable[[], Awaitable[ThreadHistoryResult]],
        **_kwargs: object,
    ) -> ThreadHistoryResult:
        return await repair()

    coordinator.run_thread_repair = AsyncMock(side_effect=run_thread_repair)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    fetched_history = thread_history_result([], is_full_history=True)

    try:
        with patch(
            "mindroom.matrix.conversation_cache.refresh_thread_history_from_source",
            AsyncMock(return_value=fetched_history),
        ) as refresh_thread_history:
            result = await asyncio.wait_for(
                conversation_cache.get_dispatch_thread_history(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="dispatch_context",
                ),
                timeout=0.2,
            )
    finally:
        await event_cache.close()

    assert result == []
    assert result.is_full_history is True
    coordinator.wait_for_thread_idle.assert_awaited_once()
    coordinator.run_thread_repair.assert_awaited_once()
    refresh_thread_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_healthy_cache_hit_does_not_enter_repair_lane(tmp_path: Path) -> None:
    """A trusted snapshot should not occupy the serialized refill lane."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    await _seed_thread_cache(
        event_cache,
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        events=[_clear_payload("$thread:localhost", body="root")],
    )
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.pending_thread_repair_deltas.return_value = ()
    coordinator.run_thread_repair = AsyncMock(side_effect=AssertionError("cache hit must not claim repair ownership"))
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        result = await conversation_cache.get_thread_history("!room:localhost", "$thread:localhost")
    finally:
        await event_cache.close()

    assert [message.event_id for message in result] == ["$thread:localhost"]
    coordinator.run_thread_repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_advisory_joiner_keeps_stale_fallback_from_strict_owner_failure(tmp_path: Path) -> None:
    """Strict and advisory refills must not share failure or stale-fallback contracts."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    await _seed_thread_cache(
        event_cache,
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        events=[_clear_payload("$thread:localhost", body="stale root")],
    )
    await event_cache.mark_thread_stale("!room:localhost", "$thread:localhost", reason="force_refetch")
    strict_scan_started = asyncio.Event()
    release_strict_scan = asyncio.Event()
    scan_count = 0
    strict_failure = RuntimeError("strict scan failed")
    advisory_failure = RuntimeError("advisory scan failed")

    async def failing_scan(*_args: object, **_kwargs: object) -> object:
        nonlocal scan_count
        scan_count += 1
        if scan_count == 1:
            strict_scan_started.set()
            await release_strict_scan.wait()
            raise strict_failure
        raise advisory_failure

    client = MagicMock()
    client.room_messages = AsyncMock(side_effect=failing_scan)
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        strict = asyncio.create_task(
            conversation_cache.get_strict_thread_history(
                "!room:localhost",
                "$thread:localhost",
                caller_label="strict_reader",
            ),
        )
        await asyncio.wait_for(strict_scan_started.wait(), timeout=1.0)
        advisory = asyncio.create_task(
            conversation_cache.get_thread_history(
                "!room:localhost",
                "$thread:localhost",
                caller_label="advisory_reader",
            ),
        )
        await asyncio.sleep(0)
        release_strict_scan.set()
        with pytest.raises(RuntimeError, match="strict scan failed"):
            await strict
        advisory_result = await asyncio.wait_for(advisory, timeout=1.0)
    finally:
        release_strict_scan.set()
        await coordinator.close()
        await event_cache.close()

    assert advisory_result.diagnostics[THREAD_HISTORY_SOURCE_DIAGNOSTIC] == THREAD_HISTORY_SOURCE_STALE_CACHE
    assert scan_count == 2


@pytest.mark.asyncio
async def test_shared_repair_logs_completion_for_each_caller(tmp_path: Path) -> None:  # noqa: PLR0915
    """Every caller should own completion telemetry even when one refill is shared."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    both_callers_waiting = asyncio.Event()
    release_idle_wait = asyncio.Event()
    both_callers_entered_repair = asyncio.Event()
    fetch_count = 0
    idle_wait_count = 0
    repair_call_count = 0
    root = ResolvedVisibleMessage.synthetic(
        sender="@user:localhost",
        body="root",
        event_id="$thread:localhost",
        content={"body": "root"},
    )

    async def fetch_snapshot(*_args: object, **_kwargs: object) -> object:
        nonlocal fetch_count
        fetch_count += 1
        fetch_started.set()
        await release_fetch.wait()
        return MagicMock(
            history=[root],
            event_sources=[_clear_payload("$thread:localhost", body="root")],
            fetch_ms=1.0,
            room_scan_pages=1,
            scanned_event_count=1,
            resolution_ms=1.0,
            sidecar_hydration_ms=0.0,
        )

    wait_for_thread_idle = coordinator.wait_for_thread_idle
    run_thread_repair = coordinator.run_thread_repair

    async def observed_wait_for_thread_idle(*args: object, **kwargs: object) -> None:
        nonlocal idle_wait_count
        await wait_for_thread_idle(*args, **kwargs)
        idle_wait_count += 1
        if idle_wait_count == 2:
            both_callers_waiting.set()
        await release_idle_wait.wait()

    async def observed_run_thread_repair(
        room_id: str,
        thread_id: str,
        repair: Callable[[], Awaitable[ThreadHistoryResult]],
        **kwargs: object,
    ) -> ThreadHistoryResult:
        nonlocal repair_call_count
        repair_call_count += 1
        if repair_call_count == 2:
            both_callers_entered_repair.set()
        return await run_thread_repair(room_id, thread_id, repair, **kwargs)

    telemetry_logger = MagicMock()
    try:
        with (
            patch("mindroom.matrix.client_thread_history.logger", telemetry_logger),
            patch.object(coordinator, "wait_for_thread_idle", side_effect=observed_wait_for_thread_idle),
            patch.object(coordinator, "run_thread_repair", side_effect=observed_run_thread_repair),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                AsyncMock(side_effect=fetch_snapshot),
            ),
        ):
            first = asyncio.create_task(
                conversation_cache.get_thread_history(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="first_reader",
                ),
            )
            second = asyncio.create_task(
                conversation_cache.get_thread_history(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="second_reader",
                ),
            )
            await asyncio.wait_for(both_callers_waiting.wait(), timeout=1.0)
            release_idle_wait.set()
            await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
            await asyncio.wait_for(both_callers_entered_repair.wait(), timeout=1.0)
            release_fetch.set()
            await asyncio.gather(first, second)
    finally:
        release_idle_wait.set()
        release_fetch.set()
        await coordinator.close()
        await event_cache.close()

    refresh_logs = [
        call
        for call in telemetry_logger.info.call_args_list
        if call.args and call.args[0] == "matrix_cache_thread_history_refreshed"
    ]
    assert fetch_count == 1
    assert [call.kwargs["caller_label"] for call in refresh_logs] == [
        "first_reader",
        "second_reader",
    ]


def test_missing_thread_repair_skips_when_writes_unavailable(tmp_path: Path) -> None:
    """A disabled durable cache should not launch futile background refill work."""
    event_cache = MagicMock()
    event_cache.principal_id = "@agent:localhost"
    event_cache.durable_writes_available = False
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = MagicMock()
    coordinator.run_thread_repair = AsyncMock()
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    with patch("mindroom.matrix.conversation_cache.create_background_task") as create_task:
        conversation_cache._schedule_missing_thread_repair("!room:localhost", "$thread:localhost")

    if create_task.called:
        create_task.call_args.args[0].close()
    create_task.assert_not_called()
    coordinator.run_thread_repair.assert_not_awaited()


def test_only_persistent_thread_repair_failure_arms_backoff(tmp_path: Path) -> None:
    """Unavailable writes should stay uncached without throttling later strict reads."""
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, MagicMock(), client=MagicMock())
    writes_unavailable = thread_history_result(
        [],
        is_full_history=True,
        diagnostics={
            "cache_store_outcome": ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE.value,
            "cache_repair_usable": False,
        },
    )
    hard_failure = thread_history_result(
        [],
        is_full_history=True,
        diagnostics={
            "cache_store_outcome": ThreadCacheReplaceOutcome.HARD_FAILURE.value,
            "cache_repair_usable": False,
        },
    )
    stale_fallback = thread_history_result(
        [],
        is_full_history=True,
        diagnostics={
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_STALE_CACHE,
            "cache_repair_usable": False,
        },
    )

    assert conversation_cache._thread_repair_result_arms_backoff(writes_unavailable) is False
    assert conversation_cache._thread_repair_result_arms_backoff(stale_fallback) is False
    assert conversation_cache._thread_repair_result_arms_backoff(hard_failure) is True


@pytest.mark.asyncio
async def test_departure_clears_retained_thread_repair_deltas(tmp_path: Path) -> None:
    """A leave/rejoin boundary must not replay plaintext retained before departure."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db", principal_id="@agent:localhost")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    room_id = "!room:localhost"
    thread_id = "$thread:localhost"
    coordinator.retain_thread_repair_delta(
        room_id,
        thread_id,
        _clear_payload("$old:localhost", body="pre-departure", thread_root_id=thread_id),
        coordination_scope=event_cache.principal_id,
    )

    try:
        assert coordinator.pending_thread_repair_deltas(
            room_id,
            thread_id,
            coordination_scope=event_cache.principal_id,
        )
        await conversation_cache.purge_rooms([room_id])
        await conversation_cache.mark_room_joined(room_id)
        retained_after_rejoin = coordinator.pending_thread_repair_deltas(
            room_id,
            thread_id,
            coordination_scope=event_cache.principal_id,
        )
    finally:
        await coordinator.close()
        await event_cache.close()

    assert retained_after_rejoin == ()


@pytest.mark.asyncio
async def test_dispatch_thread_read_degrades_when_fetcher_stalls(
    tmp_path: Path,
) -> None:
    """Dispatch-safe reads should not wait indefinitely on a direct Matrix read-through."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    release_fetch = asyncio.Event()

    async def never_returns(*_args: object, **_kwargs: object) -> ThreadHistoryResult:
        await release_fetch.wait()
        return thread_history_result([], is_full_history=False)

    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    _set_dispatch_thread_read_timeout(conversation_cache, 0.01)

    try:
        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_snapshot",
            AsyncMock(side_effect=never_returns),
        ):
            result = await asyncio.wait_for(
                conversation_cache.get_dispatch_thread_snapshot(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="dispatch_context",
                ),
                timeout=0.2,
            )
    finally:
        release_fetch.set()
        await coordinator.close()
        await event_cache.close()

    assert result == []
    assert result.is_full_history is False
    assert result.diagnostics["thread_read_degraded"] is True
    assert result.diagnostics["thread_read_error"] == "dispatch_read_timeout"
    assert result.diagnostics["thread_read_source"] == "degraded"
    assert result.diagnostics["caller_label"] == "dispatch_context"
    assert "dispatch_fetch_wait_ms" in result.diagnostics


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
async def test_dispatch_retries_strictly_after_failed_cache_repair_backoff(tmp_path: Path) -> None:
    """A proven thread should move from failed dispatch repair to fresh strict history."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db", principal_id="@mindroom_code:localhost")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    coordinator._thread_repairs = ThreadRepairRegistry(failure_backoff_seconds=0.05)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    config = conversation_cache.runtime.config
    runtime_paths = conversation_cache.runtime.runtime_paths
    matrix_id = entity_ids(config, runtime_paths)["code"]
    resolver = ConversationResolver(
        ConversationResolverDeps(
            runtime=conversation_cache.runtime,
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="code",
            matrix_id=matrix_id,
            conversation_cache=conversation_cache,
        ),
    )
    failed_history = thread_history_result(
        [],
        is_full_history=True,
        diagnostics={
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
            "cache_store_outcome": ThreadCacheReplaceOutcome.HARD_FAILURE.value,
            "cache_repair_usable": False,
        },
    )
    strict_history = thread_history_result(
        [
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="Recovered context",
                event_id="$reply:localhost",
                thread_id="$thread:localhost",
            ),
        ],
        is_full_history=True,
        diagnostics={"cache_repair_usable": True},
    )
    room = nio.MatrixRoom("!room:localhost", matrix_id.full_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Continue",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:localhost"},
            },
            "event_id": "$incoming:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": room.room_id,
            "type": "m.room.message",
        },
    )

    try:
        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_history",
            AsyncMock(side_effect=[failed_history, strict_history]),
        ) as fetch_dispatch_thread_history:
            result = await resolver.extract_dispatch_context(room, event)
    finally:
        await coordinator.close()
        await event_cache.close()

    assert result.context.is_thread is True
    assert result.context.thread_id == "$thread:localhost"
    assert result.context.thread_history is strict_history
    assert result.thread_context is not None
    assert result.thread_context.replay_guard_degraded is False
    assert fetch_dispatch_thread_history.await_count == 2


@pytest.mark.asyncio
async def test_dispatch_thread_read_uses_single_deadline_after_coordinator_wait(
    tmp_path: Path,
) -> None:
    """Dispatch fetches should not receive a fresh timeout after the coordinator wait spends the budget."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    _set_dispatch_thread_read_timeout(conversation_cache, 1.0)

    clock_values = iter([100.0, 100.0, 101.25, 101.25, 101.25, 101.25])

    def perf_counter() -> float:
        return next(clock_values, 101.25)

    try:
        with (
            patch("mindroom.matrix.cache.thread_reads.time.perf_counter", side_effect=perf_counter),
            patch(
                "mindroom.matrix.conversation_cache.fetch_dispatch_thread_snapshot",
                AsyncMock(side_effect=AssertionError("spent dispatch deadline must not start fetch")),
            ) as fetch_dispatch_thread_snapshot,
        ):
            result = await conversation_cache.get_dispatch_thread_snapshot(
                "!room:localhost",
                "$thread:localhost",
                caller_label="dispatch_context",
            )
    finally:
        await event_cache.close()

    assert result == []
    assert result.is_full_history is False
    assert result.diagnostics["thread_read_degraded"] is True
    assert result.diagnostics["thread_read_error"] == "dispatch_read_timeout"
    assert result.diagnostics["thread_read_source"] == "degraded"
    assert "dispatch_fetch_wait_ms" in result.diagnostics
    coordinator.wait_for_thread_idle.assert_awaited_once()
    fetch_dispatch_thread_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_thread_history_uses_no_stale_fetch_without_dispatch_timeout(
    tmp_path: Path,
) -> None:
    """Post-lock strict reads should wait normally but still reject stale fallback."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    async def run_thread_repair(
        _room_id: str,
        _thread_id: str,
        repair: Callable[[], Awaitable[ThreadHistoryResult]],
        **_kwargs: object,
    ) -> ThreadHistoryResult:
        return await repair()

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.pending_thread_repair_deltas.return_value = ()
    coordinator.run_thread_repair = AsyncMock(side_effect=run_thread_repair)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    fetched_history = thread_history_result([], is_full_history=True)

    try:
        with (
            patch(
                "mindroom.matrix.conversation_cache.refresh_thread_history_from_source",
                AsyncMock(return_value=fetched_history),
            ) as refresh_thread_history,
            patch(
                "mindroom.matrix.conversation_cache.fetch_thread_history",
                AsyncMock(side_effect=AssertionError("strict reads must not allow stale fallback")),
            ),
        ):
            result = await conversation_cache.get_strict_thread_history(
                "!room:localhost",
                "$thread:localhost",
                caller_label="dispatch_post_lock_refresh",
            )
    finally:
        await event_cache.close()

    assert result.is_full_history is True
    coordinator.wait_for_thread_idle.assert_awaited_once()
    coordinator.run_thread_repair.assert_awaited_once()
    refresh_thread_history.assert_awaited_once()
    assert refresh_thread_history.await_args.kwargs["allow_stale_fallback"] is False


@pytest.mark.asyncio
async def test_strict_thread_history_bypasses_repair_backoff_without_stalling(tmp_path: Path) -> None:
    """Strict model-history reads should not absorb retained repair backoff."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    failed_history = thread_history_result(
        [],
        is_full_history=True,
        diagnostics={
            "cache_store_outcome": ThreadCacheReplaceOutcome.HARD_FAILURE.value,
            "cache_repair_usable": False,
        },
    )
    fresh_history = thread_history_result(
        [ResolvedVisibleMessage.synthetic(sender="@user:localhost", body="Context", event_id="$reply")],
        is_full_history=True,
        diagnostics={"cache_repair_usable": True},
    )
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    registry = ThreadRepairRegistry(
        failure_backoff_seconds=30.0,
        max_failure_backoff_seconds=30.0,
    )
    coordinator._thread_repairs = registry
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    repair_key = (event_cache.principal_id, "!room:localhost", "$thread:localhost", True, False)
    await coordinator.run_thread_repair(
        "!room:localhost",
        "$thread:localhost",
        AsyncMock(return_value=failed_history),
        coordination_scope=event_cache.principal_id,
        hydrate_sidecars=True,
        allow_stale_fallback=False,
        result_arms_backoff=conversation_cache._thread_repair_result_arms_backoff,
    )
    assert registry.retry_after_seconds(repair_key) > 29.0

    try:
        with patch(
            "mindroom.matrix.conversation_cache.refresh_thread_history_from_source",
            AsyncMock(return_value=fresh_history),
        ) as refresh_thread_history:
            result = await asyncio.wait_for(
                conversation_cache.get_strict_thread_history(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="dispatch_post_lock_refresh",
                ),
                timeout=1.0,
            )
    finally:
        await coordinator.close()
        await event_cache.close()

    assert result == fresh_history
    assert result.is_full_history is True
    assert registry.retry_after_seconds(repair_key) == 0.0
    refresh_thread_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_thread_read_degrades_immediately_on_repair_backoff(tmp_path: Path) -> None:
    """Dispatch reads must surface repair backoff at once instead of spending their whole budget."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.run_thread_repair = AsyncMock(side_effect=ThreadRepairBackoffError(30.0))
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        result = await asyncio.wait_for(
            conversation_cache.get_dispatch_thread_history(
                "!room:localhost",
                "$thread:localhost",
                caller_label="dispatch_context",
            ),
            timeout=1.0,
        )
    finally:
        await event_cache.close()

    assert list(result) == []
    assert is_thread_history_degraded(result)
    assert result.diagnostics["thread_read_error"] == "cache_repair_backoff"
    assert result.diagnostics["cache_repair_backoff_seconds"] == 30.0
    coordinator.run_thread_repair.assert_awaited_once()


@pytest.mark.asyncio
async def test_stored_repair_releases_replayed_delta_filtered_by_redaction(tmp_path: Path) -> None:
    """A tombstone-filtered replay entered into a stored snapshot must not invalidate every later read."""
    event_cache = MagicMock()
    event_cache.principal_id = "@agent:localhost"
    event_cache.get_thread_events = AsyncMock(return_value=None)
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())
    coordinator = MagicMock()
    coordinator.pending_thread_repair_deltas.return_value = ({"event_id": "$redacted"},)

    async def run_thread_repair(
        _room_id: str,
        _thread_id: str,
        repair: Callable[[], Awaitable[ThreadHistoryResult]],
        **_kwargs: object,
    ) -> ThreadHistoryResult:
        return await repair()

    async def refresh(
        _client: object,
        _room_id: str,
        _thread_id: str,
        _event_cache: object,
        *,
        retained_event_sources: RetainedThreadEventSourceProvider,
        **_kwargs: object,
    ) -> ThreadHistoryResult:
        assert [source["event_id"] for source in retained_event_sources.current_event_sources()] == ["$redacted"]
        retained_event_sources.record_replayed_event_ids({"$redacted"})
        return thread_history_result(
            [],
            is_full_history=True,
            diagnostics={
                "cache_store_outcome": ThreadCacheReplaceOutcome.STORED.value,
                "cache_repair_usable": True,
                "thread_read_source": "homeserver",
            },
        )

    coordinator.run_thread_repair = AsyncMock(side_effect=run_thread_repair)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    conversation_cache._write_cache_ops.invalidate_known_thread = AsyncMock()

    with patch("mindroom.matrix.conversation_cache.refresh_thread_history_from_source", side_effect=refresh):
        result = await conversation_cache._fetch_thread_from_client(
            fetch_thread_history,
            "!room:localhost",
            "$thread:localhost",
            caller_label="redaction_repair",
            coordinator_queue_wait_ms=0.0,
            wants_full_history=True,
            allows_stale_fallback=False,
            bypass_repair_backoff=False,
        )

    assert result.is_full_history is True
    coordinator.acknowledge_thread_repair_deltas.assert_called_once_with(
        "!room:localhost",
        "$thread:localhost",
        {"$redacted"},
        coordination_scope="@agent:localhost",
    )
    event_cache.get_thread_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_strict_history_bypasses_inherited_turn_memoization(tmp_path: Path) -> None:
    """Background tasks should see post-delivery history despite copied ContextVars."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=MagicMock())

    async def run_thread_repair(
        _room_id: str,
        _thread_id: str,
        repair: Callable[[], Awaitable[ThreadHistoryResult]],
        **_kwargs: object,
    ) -> ThreadHistoryResult:
        return await repair()

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(return_value=None)
    coordinator.pending_thread_repair_deltas.return_value = ()
    coordinator.run_thread_repair = AsyncMock(side_effect=run_thread_repair)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    before_delivery = thread_history_result(
        [ResolvedVisibleMessage.synthetic(sender="@user:localhost", body="Question", event_id="$question")],
        is_full_history=True,
        diagnostics={"cache_repair_usable": True},
    )
    after_delivery = thread_history_result(
        [
            *before_delivery,
            ResolvedVisibleMessage.synthetic(sender="@bot:localhost", body="Answer", event_id="$answer"),
        ],
        is_full_history=True,
        diagnostics={"cache_repair_usable": True},
    )

    try:
        with patch(
            "mindroom.matrix.conversation_cache.fetch_dispatch_thread_history",
            new=AsyncMock(side_effect=[before_delivery, after_delivery]),
        ) as fetch:
            async with conversation_cache.turn_scope():
                first = await conversation_cache.get_strict_thread_history("!room:localhost", "$thread")
                inherited = await asyncio.create_task(
                    conversation_cache.get_strict_thread_history("!room:localhost", "$thread"),
                )
                fresh = await asyncio.create_task(
                    conversation_cache.get_fresh_strict_thread_history("!room:localhost", "$thread"),
                )
    finally:
        await event_cache.close()

    assert [message.event_id for message in first] == ["$question"]
    assert [message.event_id for message in inherited] == ["$question"]
    assert [message.event_id for message in fresh] == ["$question", "$answer"]
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_strict_source_refresh_bypasses_usable_cache(
    tmp_path: Path,
) -> None:
    """Explicit source refresh should serialize one Matrix fetch without accepting a cache hit."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = object()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)
    coordinator = EventCacheWriteCoordinator(logger=conversation_cache.logger)
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    barrier_entered = asyncio.Event()
    release_write = asyncio.Event()
    write_started = asyncio.Event()
    wait_for_thread_idle = coordinator.wait_for_thread_idle

    async def pending_cache_write() -> None:
        write_started.set()
        await release_write.wait()

    async def observed_wait_for_thread_idle(
        room_id: str,
        thread_id: str,
        *,
        ignore_cancelled_room_fences: bool = False,
        coordination_scope: str,
    ) -> None:
        barrier_entered.set()
        await wait_for_thread_idle(
            room_id,
            thread_id,
            ignore_cancelled_room_fences=ignore_cancelled_room_fences,
            coordination_scope=coordination_scope,
        )

    pending_write_task = coordinator.queue_thread_update(
        "!room:localhost",
        "$thread:localhost",
        pending_cache_write,
        name="matrix_cache_pending_source_refresh_test_write",
        coordination_scope=event_cache.principal_id,
    )
    fetched_history = thread_history_result(
        [ResolvedVisibleMessage.synthetic(sender="@bot:localhost", body="Target", event_id="$target")],
        is_full_history=True,
    )

    try:
        await asyncio.wait_for(write_started.wait(), timeout=5.0)
        with (
            patch.object(
                coordinator,
                "wait_for_thread_idle",
                side_effect=observed_wait_for_thread_idle,
            ),
            patch(
                "mindroom.matrix.conversation_cache.fetch_dispatch_thread_history",
                AsyncMock(side_effect=AssertionError("source refresh must bypass cache selection")),
            ) as cache_thread_history,
            patch(
                "mindroom.matrix.conversation_cache.refresh_thread_history_from_source",
                AsyncMock(return_value=fetched_history),
            ) as refresh_thread_history,
        ):
            read_task = asyncio.create_task(
                conversation_cache.refresh_strict_thread_history_from_source(
                    "!room:localhost",
                    "$thread:localhost",
                    caller_label="startup_auto_resume_freshness",
                ),
            )
            await asyncio.wait_for(barrier_entered.wait(), timeout=5.0)
            refresh_thread_history.assert_not_awaited()
            release_write.set()
            result = await asyncio.wait_for(read_task, timeout=5.0)
    finally:
        release_write.set()
        await pending_write_task
        await coordinator.close()
        await event_cache.close()

    assert [message.event_id for message in result] == ["$target"]
    assert result.is_full_history is True
    refresh_thread_history.assert_awaited_once()
    assert refresh_thread_history.await_args.args[:4] == (
        client,
        "!room:localhost",
        "$thread:localhost",
        event_cache,
    )
    assert refresh_thread_history.await_args.kwargs["allow_stale_fallback"] is False
    cache_thread_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_thread_history_propagates_cache_coordinator_timeout(
    tmp_path: Path,
) -> None:
    """Post-lock strict reads must not be converted into degraded dispatch results."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    coordinator = MagicMock()
    coordinator.wait_for_thread_idle = AsyncMock(side_effect=TimeoutError("strict wait timed out"))
    coordinator.run_thread_repair = AsyncMock(side_effect=AssertionError("strict read should not fetch after timeout"))
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        with pytest.raises(TimeoutError, match="strict wait timed out"):
            await conversation_cache.get_strict_thread_history(
                "!room:localhost",
                "$thread:localhost",
                caller_label="dispatch_post_lock_refresh",
            )
    finally:
        await event_cache.close()

    coordinator.wait_for_thread_idle.assert_awaited_once()
    coordinator.run_thread_repair.assert_not_awaited()


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
async def test_thread_snapshot_storage_exposes_direct_cache_state_reads(tmp_path: Path) -> None:
    """Thread snapshot ownership should expose joined thread and room cache state."""
    db, _maintenance_report, _generation = await event_cache_module._initialize_event_cache_db(
        tmp_path / "event_cache.db",
    )

    try:
        await sqlite_event_cache_threads._replace_thread_locked(
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
            validated_at=100.0,
        )
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await sqlite_event_cache_threads.mark_thread_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="thread_stale",
            )
            await sqlite_event_cache_threads.mark_room_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="room_stale",
            )
        await db.commit()

        state = await sqlite_event_cache_threads.load_thread_cache_state(
            db,
            principal_id="__mindroom_default_principal__",
            room_id="!room:localhost",
            thread_id="$thread_root",
        )
    finally:
        await db.close()

    assert state is not None
    assert state.validated_at == 100.0
    assert state.invalidated_at == 200.0
    assert state.invalidation_reason == "thread_stale"
    assert state.room_invalidated_at == 200.0
    assert state.room_invalidation_reason == "room_stale"
    assert thread_cache_rejection_reason(state) == "thread_invalidated_after_validation"


@pytest.mark.asyncio
async def test_sqlite_stale_markers_are_monotonic(tmp_path: Path) -> None:
    """Older stale markers should not downgrade newer thread or room invalidations."""
    db, _maintenance_report, _generation = await event_cache_module._initialize_event_cache_db(
        tmp_path / "event_cache.db",
    )

    try:
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await sqlite_event_cache_threads.mark_thread_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="newer_thread_marker",
            )
            await sqlite_event_cache_threads.mark_room_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="newer_room_marker",
            )
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=100.0):
            await sqlite_event_cache_threads.mark_thread_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                thread_id="$thread_root",
                reason="older_thread_marker",
            )
            await sqlite_event_cache_threads.mark_room_stale_locked(
                db,
                principal_id="__mindroom_default_principal__",
                room_id="!room:localhost",
                reason="older_room_marker",
            )
        await db.commit()

        state = await sqlite_event_cache_threads.load_thread_cache_state(
            db,
            principal_id="__mindroom_default_principal__",
            room_id="!room:localhost",
            thread_id="$thread_root",
        )
    finally:
        await db.close()

    assert state is not None
    assert state.invalidated_at == 200.0
    assert state.invalidation_reason == "newer_thread_marker"
    assert state.room_invalidated_at == 200.0
    assert state.room_invalidation_reason == "newer_room_marker"


def _thread_cache_state(
    *,
    validated_at: float | None = None,
    invalidated_at: float | None = None,
    invalidation_reason: str | None = None,
    room_invalidated_at: float | None = None,
    room_invalidation_reason: str | None = None,
) -> ThreadCacheState:
    return ThreadCacheState(
        validated_at=validated_at,
        invalidated_at=invalidated_at,
        invalidation_reason=invalidation_reason,
        room_invalidated_at=room_invalidated_at,
        room_invalidation_reason=room_invalidation_reason,
    )


@pytest.mark.parametrize(
    ("cache_state", "expected_reason"),
    [
        pytest.param(None, "no_cache_state", id="missing_state_rejects"),
        pytest.param(
            _thread_cache_state(invalidated_at=100.0, invalidation_reason="live_thread_mutation"),
            "cache_never_validated",
            id="never_validated_rejects",
        ),
        pytest.param(
            _thread_cache_state(validated_at=100.0, invalidated_at=100.0, invalidation_reason="tie"),
            "thread_invalidated_after_validation",
            id="thread_invalidation_tie_rejects",
        ),
        pytest.param(
            _thread_cache_state(validated_at=100.0, room_invalidated_at=100.0, room_invalidation_reason="tie"),
            "room_invalidated_after_validation",
            id="room_invalidation_tie_rejects",
        ),
        pytest.param(
            _thread_cache_state(validated_at=200.0, invalidated_at=100.0, invalidation_reason="superseded"),
            None,
            id="invalidation_before_validation_accepts",
        ),
        pytest.param(
            _thread_cache_state(validated_at=200.0, room_invalidated_at=100.0, room_invalidation_reason="superseded"),
            None,
            id="room_invalidation_before_validation_accepts",
        ),
        # PR #731 removed the age rule and PR #734 removed the restart rule: an arbitrarily old
        # validation stays trusted until an invalidation marker lands at or after it.
        pytest.param(
            _thread_cache_state(validated_at=1.0),
            None,
            id="ancient_validation_accepts",
        ),
    ],
)
def test_thread_cache_rejection_reason_rule_table(
    cache_state: ThreadCacheState | None,
    expected_reason: str | None,
) -> None:
    """The durable trust gate must reject exactly on missing/never-validated/invalidated-at-or-after state."""
    assert thread_cache_rejection_reason(cache_state) == expected_reason


@pytest.mark.asyncio
async def test_replace_thread_if_not_newer_refuses_after_midflight_invalidation(tmp_path: Path) -> None:
    """A fetch that raced with a thread or room invalidation must not bury the newer stale marker."""
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
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source], validated_at=100.0)
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="live_thread_mutation")

        replaced_behind_marker = await cache.replace_thread_if_not_newer(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=150.0,
            validated_at=300.0,
        )
        state_after_refusal = await cache.get_thread_cache_state("!room:localhost", "$thread_root")

        replaced_after_marker = await cache.replace_thread_if_not_newer(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=250.0,
            validated_at=300.0,
        )
        state_after_replace = await cache.get_thread_cache_state("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert replaced_behind_marker is ThreadCacheReplaceOutcome.INVALIDATED
    assert state_after_refusal is not None
    assert state_after_refusal.invalidated_at == 200.0
    assert thread_cache_rejection_reason(state_after_refusal) == "thread_invalidated_after_validation"

    assert replaced_after_marker is ThreadCacheReplaceOutcome.STORED
    assert state_after_replace is not None
    # The stored validation time is clamped to fetch start, so an invalidation landing during the
    # fetch still outranks this snapshot at read time even if it slipped past the replace guard.
    assert state_after_replace.validated_at == 250.0
    assert state_after_replace.invalidated_at is None
    assert thread_cache_rejection_reason(state_after_replace) is None


@pytest.mark.asyncio
async def test_replace_thread_if_not_newer_refuses_after_midflight_room_invalidation(tmp_path: Path) -> None:
    """A room-wide stale marker that landed after fetch start must also refuse snapshot replacement."""
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
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source], validated_at=100.0)
        with patch("mindroom.matrix.cache.sqlite_event_cache_threads.time.time", return_value=200.0):
            await cache.mark_room_threads_stale("!room:localhost", reason="sync_thread_lookup_unavailable")

        replaced = await cache.replace_thread_if_not_newer(
            "!room:localhost",
            "$thread_root",
            [root_source],
            expected_membership_epoch=await cache.room_membership_epoch("!room:localhost"),
            fetch_started_at=150.0,
            validated_at=300.0,
        )
        state = await cache.get_thread_cache_state("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert replaced is ThreadCacheReplaceOutcome.INVALIDATED
    assert state is not None
    assert state.room_invalidated_at == 200.0
    assert thread_cache_rejection_reason(state) == "room_invalidated_after_validation"


@pytest.mark.asyncio
async def test_incremental_revalidation_requires_incremental_invalidation_reason(tmp_path: Path) -> None:
    """Appends may only clear invalidations caused by incremental mutations, never other reasons."""
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
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source], validated_at=100.0)

        not_invalidated = await cache.revalidate_thread_after_incremental_update("!room:localhost", "$thread_root")

        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="live_append_failed")
        non_incremental = await cache.revalidate_thread_after_incremental_update("!room:localhost", "$thread_root")
        state_after_non_incremental = await cache.get_thread_cache_state("!room:localhost", "$thread_root")

        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="live_thread_mutation")
        weakened = await cache.revalidate_thread_after_incremental_update("!room:localhost", "$thread_root")
        state_after_weakening_attempt = await cache.get_thread_cache_state("!room:localhost", "$thread_root")

        await _replace_thread(cache, "!room:localhost", "$thread_root", [root_source])
        await cache.mark_thread_stale("!room:localhost", "$thread_root", reason="live_thread_mutation")
        incremental = await cache.revalidate_thread_after_incremental_update("!room:localhost", "$thread_root")
        state_after_incremental = await cache.get_thread_cache_state("!room:localhost", "$thread_root")
    finally:
        await cache.close()

    assert not_invalidated is False
    assert non_incremental is False
    assert state_after_non_incremental is not None
    assert thread_cache_rejection_reason(state_after_non_incremental) == "thread_invalidated_after_validation"
    assert weakened is False
    assert state_after_weakening_attempt is not None
    assert state_after_weakening_attempt.invalidation_reason == "live_append_failed"
    assert thread_cache_rejection_reason(state_after_weakening_attempt) == "thread_invalidated_after_validation"
    assert incremental is True
    assert state_after_incremental is not None
    assert thread_cache_rejection_reason(state_after_incremental) is None


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
    sender: str = "@user:localhost",
    room_id: str | None = None,
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
    payload: dict[str, object] = {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "type": "m.room.message",
        "content": content,
    }
    if room_id is not None:
        payload["room_id"] = room_id
    return payload


def _make_text_event_with_bundled_edit(
    *,
    room_id: str,
    event_id: str,
    sender: str,
    body: str,
    edit_id: str,
    edited_body: str,
) -> MagicMock:
    """Return one complete text event carrying a clear bundled replacement."""
    event = _make_text_event(
        event_id=event_id,
        sender=sender,
        body=body,
        server_timestamp=2000,
        source_content={"body": body},
    )
    event.source["room_id"] = room_id
    event.source["unsigned"] = {
        "m.relations": {
            "m.replace": _clear_payload(
                edit_id,
                body=edited_body,
                sender=sender,
                room_id=room_id,
                edit_of=event_id,
                origin_server_ts=3000,
            ),
        },
    }
    return event


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
@pytest.mark.parametrize("upgrade_kind", ["encrypted", "provisional"])
@pytest.mark.parametrize("batch_order", [("stale", "canonical"), ("canonical", "stale")])
async def test_duplicate_ids_in_one_batch_derive_only_final_canonical_state(
    event_cache: ConversationEventCache,
    upgrade_kind: str,
    batch_order: tuple[str, str],
) -> None:
    """Intermediate same-ID representations cannot leave derived cache state."""
    room_id = "!room:localhost"
    event_id = "$same:localhost"
    old_root_id = "$old-root:localhost"
    new_root_id = "$new-root:localhost"
    old_mxc = "mxc://server/old"
    new_mxc = "mxc://server/new"

    canonical = _clear_payload(
        event_id,
        body="canonical",
        room_id=room_id,
        thread_root_id=new_root_id,
    )
    canonical["content"].update(
        {
            "msgtype": "m.file",
            "url": new_mxc,
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
        },
    )
    if upgrade_kind == "encrypted":
        stale = _opaque_payload(event_id, thread_root_id=old_root_id)
        stale["room_id"] = room_id
    else:
        stale = _clear_payload(
            event_id,
            body="provisional",
            room_id=room_id,
            thread_root_id=old_root_id,
        )
        stale["content"].update(
            {
                "msgtype": "m.file",
                "url": old_mxc,
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
            },
        )
        stale = event_normalization.mark_provisional_outbound_event(stale)
    payloads = {"stale": stale, "canonical": canonical}

    await event_cache.store_events_batch(
        [(event_id, room_id, payloads[payload_kind]) for payload_kind in batch_order],
    )

    cached = await event_cache.get_event(room_id, event_id)
    assert cached is not None
    assert cached["content"]["body"] == "canonical"
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == new_root_id
    assert await event_cache.get_thread_id_for_event(room_id, new_root_id) == new_root_id
    assert await event_cache.get_thread_id_for_event(room_id, old_root_id) is None
    assert not await event_cache.store_mxc_text(room_id, event_id, old_mxc, "stale")
    assert await event_cache.store_mxc_text(room_id, event_id, new_mxc, "canonical")


@pytest.mark.asyncio
@pytest.mark.parametrize("upgrade_kind", ["encrypted", "provisional"])
@pytest.mark.parametrize("batch_order", [("stale", "canonical"), ("canonical", "stale")])
async def test_duplicate_ids_in_one_batch_discard_superseded_bundles(
    event_cache: ConversationEventCache,
    upgrade_kind: str,
    batch_order: tuple[str, str],
) -> None:
    """Bundles from a discarded top-level view cannot quarantine a valid explicit event."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    canonical = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    stale_edit = _clear_payload(
        edit_id,
        body="Stale",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=2000,
    )
    if upgrade_kind == "encrypted":
        stale = _opaque_payload(original_id, origin_server_ts=1000)
        stale["room_id"] = room_id
    else:
        stale = event_normalization.mark_provisional_outbound_event(
            _clear_payload(
                original_id,
                body="Provisional",
                room_id=room_id,
                origin_server_ts=1000,
            ),
        )
    stale["unsigned"] = {"m.relations": {"m.replace": stale_edit}}
    explicit_edit = _clear_payload(
        edit_id,
        body="Real",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=2000,
    )
    payloads = {"stale": stale, "canonical": canonical}

    await event_cache.store_events_batch(
        [
            *((original_id, room_id, payloads[payload_kind]) for payload_kind in batch_order),
            (edit_id, room_id, explicit_edit),
        ],
    )

    cached_original = await event_cache.get_event(room_id, original_id)
    cached_edit = await event_cache.get_event(room_id, edit_id)
    assert cached_original is not None
    assert cached_original["content"]["body"] == "Original"
    assert cached_edit is not None
    assert cached_edit["content"]["m.new_content"]["body"] == "Real"
    latest = await event_cache.get_latest_edit(
        room_id,
        cached_original,
        validator=valid_room_message_replacement,
    )
    assert latest is not None
    assert latest["event_id"] == edit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("promotion_write", ["point_store", "thread_append"])
async def test_promoting_indexed_rich_reply_root_invalidates_old_parent_snapshot(
    event_cache: ConversationEventCache,
    promotion_write: str,
) -> None:
    """A newly proven rich-reply root must make its old parent snapshot unusable."""
    room_id = "!room:localhost"
    parent_id = "$parent:localhost"
    rich_reply_id = "$rich-reply:localhost"
    rich_reply = _clear_payload(
        rich_reply_id,
        body="Rich reply",
        origin_server_ts=3000,
    )
    rich_reply["content"] = {
        "body": "Rich reply",
        "msgtype": "m.text",
        "m.relates_to": {"m.in_reply_to": {"event_id": parent_id}},
    }
    await _replace_thread(
        event_cache,
        room_id,
        parent_id,
        [
            _clear_payload(parent_id, body="Parent", origin_server_ts=1000),
            _clear_payload(
                "$parent-child:localhost",
                body="Parent child",
                thread_root_id=parent_id,
                origin_server_ts=2000,
            ),
            rich_reply,
        ],
    )
    assert await event_cache.get_thread_id_for_event(room_id, rich_reply_id) == parent_id

    promoted_child_id = "$rich-reply-child:localhost"
    promoted_child = _clear_payload(
        promoted_child_id,
        body="Rich reply child",
        thread_root_id=rich_reply_id,
        origin_server_ts=4000,
    )
    if promotion_write == "point_store":
        await event_cache.store_event(promoted_child_id, room_id, promoted_child)
    else:
        assert not await event_cache.append_event(room_id, rich_reply_id, promoted_child)

    assert await event_cache.get_thread_id_for_event(room_id, rich_reply_id) == rich_reply_id
    parent_state = await event_cache.get_thread_cache_state(room_id, parent_id)
    assert thread_cache_rejection_reason(parent_state) == "thread_invalidated_after_validation"


@pytest.mark.asyncio
async def test_edit_follows_promoted_ancestor_before_stale_descendant_index(
    event_cache: ConversationEventCache,
) -> None:
    """Current reply ancestry must outrank a stale inherited membership index."""
    room_id = "!room:localhost"
    old_root_id = "$old-root:localhost"
    promoted_id = "$promoted:localhost"
    descendant_id = "$descendant:localhost"
    edit_id = "$edit:localhost"

    def plain_reply(event_id: str, target_id: str, timestamp: int) -> dict[str, object]:
        source = _clear_payload(event_id, origin_server_ts=timestamp)
        source["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": target_id}}
        return source

    await _replace_thread(
        event_cache,
        room_id,
        old_root_id,
        [
            _clear_payload(old_root_id, body="Old root", origin_server_ts=1000),
            plain_reply(promoted_id, old_root_id, 2000),
            plain_reply(descendant_id, promoted_id, 3000),
        ],
    )
    await event_cache.store_event(
        "$promoted-child:localhost",
        room_id,
        _clear_payload(
            "$promoted-child:localhost",
            thread_root_id=promoted_id,
            origin_server_ts=4000,
        ),
    )
    edit_source = _clear_payload(
        edit_id,
        body="Edited descendant",
        edit_of=descendant_id,
        origin_server_ts=5000,
    )

    assert await event_cache.get_thread_id_for_event(room_id, promoted_id) == promoted_id
    assert await event_cache.get_thread_id_for_event(room_id, descendant_id) == old_root_id

    async def fetch_event_source(fetch_room_id: str, event_id: str) -> dict[str, object] | None:
        assert fetch_room_id == room_id
        return await event_cache.get_event(fetch_room_id, event_id)

    async def fetch_event_info(fetch_room_id: str, event_id: str) -> EventInfo | None:
        source = await fetch_event_source(fetch_room_id, event_id)
        return None if source is None else EventInfo.from_event(source)

    async def prove_thread_root(_room_id: str, event_id: str) -> ThreadRootProof:
        return ThreadRootProof.proven() if event_id == promoted_id else ThreadRootProof.not_a_thread_root()

    resolution = await resolve_event_thread_membership(
        room_id,
        EventInfo.from_event(edit_source),
        event_id=edit_id,
        event_source=edit_source,
        access=conversation_relation_thread_membership_access(
            ThreadMembershipAccess(
                lookup_thread_id=event_cache.get_thread_id_for_event,
                fetch_event_info=fetch_event_info,
                prove_thread_root=prove_thread_root,
                fetch_event_source=fetch_event_source,
            ),
        ),
    )

    assert resolution.state is ThreadResolutionState.THREADED
    assert resolution.thread_id == promoted_id


@pytest.mark.asyncio
async def test_conflicting_duplicate_replacement_identity_is_rejected_by_cache(
    event_cache: ConversationEventCache,
) -> None:
    """SQLite and PostgreSQL reject conflicting payloads sharing one edit event ID."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$same-edit:localhost"

    def edit(body: str) -> dict[str, object]:
        return {
            "event_id": edit_id,
            "sender": "@user:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
            },
        }

    bundled = edit("Bundled")
    explicit = edit("Explicit")
    original = _clear_payload(original_id, body="Original")
    original["unsigned"] = {"m.relations": {"m.replace": bundled}}
    await event_cache.store_events_batch(
        [
            (original_id, room_id, original),
            (edit_id, room_id, explicit),
        ],
    )

    assert (
        await event_cache.get_latest_edit(
            room_id,
            original,
            validator=valid_room_message_replacement,
        )
        is None
    )


@pytest.mark.asyncio
async def test_bundled_self_replacement_does_not_quarantine_original(
    event_cache: ConversationEventCache,
) -> None:
    """A malformed bundle cannot reuse and tombstone its container's identity."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    original["unsigned"] = {
        "m.relations": {
            "m.replace": _clear_payload(
                original_id,
                body="Forged",
                room_id=room_id,
                edit_of=original_id,
                origin_server_ts=2000,
            ),
        },
    }

    await event_cache.store_event(original_id, room_id, original)

    cached = await event_cache.get_event(room_id, original_id)
    assert cached is not None
    assert cached["content"]["body"] == "Original"
    assert await event_cache.redacted_event_ids(room_id, {original_id}) == set()
    assert (
        await event_cache.get_latest_edit(
            room_id,
            cached,
            validator=valid_room_message_replacement,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidity", ["state", "other-room", "sender", "type"])
@pytest.mark.parametrize(
    "representation_order",
    [("bundled", "explicit"), ("explicit", "bundled")],
)
async def test_invalid_bundled_identity_does_not_quarantine_valid_explicit_edit(
    event_cache: ConversationEventCache,
    invalidity: str,
    representation_order: tuple[str, str],
) -> None:
    """Out-of-scope bundled views cannot poison a valid immutable edit identity."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    valid_edit = _clear_payload(
        edit_id,
        body="Valid",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=2000,
    )
    invalid_bundle = _clear_payload(
        edit_id,
        body="Forged",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=2000,
    )
    if invalidity == "state":
        invalid_bundle["state_key"] = ""
    elif invalidity == "other-room":
        invalid_bundle["room_id"] = "!other:localhost"
    elif invalidity == "sender":
        invalid_bundle["sender"] = "@mallory:localhost"
    else:
        invalid_bundle["type"] = "m.reaction"
    original["unsigned"] = {"m.relations": {"m.replace": invalid_bundle}}
    batches = {
        "bundled": [(original_id, room_id, original)],
        "explicit": [(edit_id, room_id, valid_edit)],
    }

    for representation in representation_order:
        await event_cache.store_events_batch(batches[representation])

    assert await event_cache.redacted_event_ids(room_id, {edit_id}) == set()
    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )
    assert latest is not None
    assert latest["event_id"] == edit_id
    assert latest["content"]["m.new_content"]["body"] == "Valid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "representation_order",
    [("bundled", "explicit"), ("explicit", "bundled")],
)
async def test_conflicting_replacement_identity_across_originals_is_quarantined(
    event_cache: ConversationEventCache,
    representation_order: tuple[str, str],
) -> None:
    """Both caches quarantine cross-original edit identities in either arrival order."""
    room_id = "!room:localhost"
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    edit_id = "$same-edit:localhost"
    sender = "@user:localhost"

    def edit(target_id: str, body: str) -> dict[str, object]:
        return {
            "event_id": edit_id,
            "room_id": room_id,
            "sender": sender,
            "origin_server_ts": 3000,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": target_id},
            },
        }

    plain_root = _clear_payload(root_id, body="Root", origin_server_ts=1000)
    root = json.loads(json.dumps(plain_root))
    root["unsigned"] = {"m.relations": {"m.replace": edit(root_id, "Forged root")}}
    reply = _clear_payload(
        reply_id,
        body="Reply",
        thread_root_id=root_id,
        origin_server_ts=2000,
    )
    explicit = edit(reply_id, "Forged reply")
    batches = {
        "bundled": [(root_id, room_id, root), (reply_id, room_id, reply)],
        "explicit": [(edit_id, room_id, explicit)],
    }
    await event_cache.store_events_batch(
        [(root_id, room_id, plain_root), (reply_id, room_id, reply)],
    )
    for representation in representation_order:
        await event_cache.store_events_batch(batches[representation])

    assert await event_cache.get_event(room_id, edit_id) is None
    assert await event_cache.redacted_event_ids(room_id, {edit_id}) == {edit_id}
    cached_root = await event_cache.get_event(room_id, root_id)
    cached_reply = await event_cache.get_event(room_id, reply_id)
    assert cached_root is not None
    assert cached_reply is not None
    assert "m.replace" not in cached_root.get("unsigned", {}).get("m.relations", {})
    for original in (cached_root, cached_reply):
        assert (
            await event_cache.get_latest_edit(
                room_id,
                original,
                validator=valid_room_message_replacement,
            )
            is None
        )


@pytest.mark.asyncio
async def test_conflicting_cached_and_bundled_newest_edit_falls_back_to_older(
    event_cache: ConversationEventCache,
) -> None:
    """A conflicting newest identity must not hide an older valid replacement."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    newest_id = "$newest:localhost"

    def edit(event_id: str, body: str, timestamp: int) -> dict[str, object]:
        return {
            "event_id": event_id,
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": timestamp,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
            },
        }

    original = _clear_payload(original_id, body="Original")
    original["unsigned"] = {
        "m.relations": {
            "m.replace": edit(newest_id, "Bundled conflict", 3000),
        },
    }
    await event_cache.store_events_batch(
        [
            (original_id, room_id, original),
            ("$older:localhost", room_id, edit("$older:localhost", "Older valid", 2000)),
            (newest_id, room_id, edit(newest_id, "Explicit conflict", 3000)),
        ],
    )

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    assert latest is not None
    assert latest["event_id"] == "$older:localhost"
    assert latest["content"]["m.new_content"]["body"] == "Older valid"
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        room_id,
        None,
        "@user:localhost",
        runtime_started_at=None,
    )
    assert snapshot is not None
    assert snapshot.content["body"] == "Older valid"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_older_valid_edit", [False, True])
async def test_unstored_bundled_original_rejects_cached_cross_target_identity(
    event_cache: ConversationEventCache,
    with_older_valid_edit: bool,
) -> None:
    """A cached edit identity for another target must not validate an unstored bundle."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    other_target_id = "$other:localhost"
    conflict_id = "$conflict:localhost"
    older_id = "$older:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    bundled_conflict = _clear_payload(
        conflict_id,
        body="Bundled forged",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=3000,
    )
    original["unsigned"] = {"m.relations": {"m.replace": bundled_conflict}}
    cached_conflict = _clear_payload(
        conflict_id,
        body="Cached other target",
        room_id=room_id,
        edit_of=other_target_id,
        origin_server_ts=3000,
    )
    cache_rows = [(conflict_id, room_id, cached_conflict)]
    if with_older_valid_edit:
        older = _clear_payload(
            older_id,
            body="Older valid",
            room_id=room_id,
            edit_of=original_id,
            origin_server_ts=2000,
        )
        cache_rows.append((older_id, room_id, older))
    await event_cache.store_events_batch(cache_rows)

    assert await event_cache.get_event(room_id, original_id) is None
    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    if with_older_valid_edit:
        assert latest is not None
        assert latest["event_id"] == older_id
        assert latest["content"]["m.new_content"]["body"] == "Older valid"
    else:
        assert latest is None


@pytest.mark.asyncio
async def test_unstored_bundle_rejects_conflicting_cached_bundle_identity(
    event_cache: ConversationEventCache,
) -> None:
    """Cached bundled identities must conflict with incoming bundled identities."""
    room_id = "!room:localhost"
    sender = "@user:localhost"
    edit_id = "$same-edit:localhost"

    def original(event_id: str, body: str, edited_body: str) -> dict[str, object]:
        event = _clear_payload(
            event_id,
            body=body,
            sender=sender,
            room_id=room_id,
            origin_server_ts=1000,
        )
        event["unsigned"] = {
            "m.relations": {
                "m.replace": _clear_payload(
                    edit_id,
                    body=edited_body,
                    sender=sender,
                    room_id=room_id,
                    edit_of=event_id,
                    origin_server_ts=2000,
                ),
            },
        }
        return event

    cached_other = original("$other:localhost", "Other", "Cached other")
    incoming = original("$wanted:localhost", "Wanted", "Forged wanted")
    await event_cache.store_event("$other:localhost", room_id, cached_other)

    assert await event_cache.get_event(room_id, edit_id) is None
    assert (
        await event_cache.get_latest_edit(
            room_id,
            incoming,
            validator=valid_room_message_replacement,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unstored_bundled_original_accepts_cached_encrypted_representation(
    event_cache: ConversationEventCache,
) -> None:
    """A cached ciphertext view must not make its clear bundled replacement contradictory."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    clear_edit = _clear_payload(
        edit_id,
        body="Bundled clear",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=3000,
    )
    original["unsigned"] = {"m.relations": {"m.replace": clear_edit}}
    encrypted_edit = _opaque_payload(edit_id, origin_server_ts=3000)
    encrypted_edit["room_id"] = room_id
    await event_cache.store_event(edit_id, room_id, encrypted_edit)

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    assert latest is not None
    assert latest["event_id"] == edit_id
    assert latest["content"]["m.new_content"]["body"] == "Bundled clear"


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_kind", ["encrypted", "provisional"])
async def test_unstored_bundle_rejects_cached_upgrade_with_different_target(
    event_cache: ConversationEventCache,
    cached_kind: str,
) -> None:
    """A legal representation upgrade cannot change an exposed replacement target."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    other_target_id = "$other:localhost"
    edit_id = "$edit:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    bundled_clear = _clear_payload(
        edit_id,
        body="Bundled forged",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=3000,
    )
    original["unsigned"] = {"m.relations": {"m.replace": bundled_clear}}
    if cached_kind == "encrypted":
        cached = _opaque_payload(edit_id, origin_server_ts=3000)
        cached["room_id"] = room_id
        cached["content"]["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": other_target_id,
        }
    else:
        cached = event_normalization.mark_provisional_outbound_event(
            _clear_payload(
                edit_id,
                body="Provisional other target",
                room_id=room_id,
                edit_of=other_target_id,
                origin_server_ts=3000,
            ),
        )
    await event_cache.store_event(edit_id, room_id, cached)

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    assert latest is None


@pytest.mark.asyncio
async def test_unstored_encrypted_bundle_accepts_cached_clear_representation(
    event_cache: ConversationEventCache,
) -> None:
    """A clear cached edit must supersede its same-target opaque bundled view."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    opaque_bundle = _opaque_payload(edit_id, origin_server_ts=3000)
    opaque_bundle["room_id"] = room_id
    opaque_bundle["content"]["m.relates_to"] = {
        "rel_type": "m.replace",
        "event_id": original_id,
    }
    original["unsigned"] = {"m.relations": {"m.replace": opaque_bundle}}
    clear_edit = _clear_payload(
        edit_id,
        body="Cached clear",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=3000,
    )
    await event_cache.store_event(edit_id, room_id, clear_edit)

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    assert latest is not None
    assert latest["event_id"] == edit_id
    assert latest["content"]["m.new_content"]["body"] == "Cached clear"


@pytest.mark.asyncio
async def test_unstored_canonical_bundle_supersedes_cached_provisional_representation(
    event_cache: ConversationEventCache,
) -> None:
    """A canonical bundle must supersede its same-target provisional cached view."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$edit:localhost"
    original = _clear_payload(
        original_id,
        body="Original",
        room_id=room_id,
        origin_server_ts=1000,
    )
    canonical_edit = _clear_payload(
        edit_id,
        body="Canonical",
        room_id=room_id,
        edit_of=original_id,
        origin_server_ts=3000,
    )
    original["unsigned"] = {"m.relations": {"m.replace": canonical_edit}}
    provisional_edit = event_normalization.mark_provisional_outbound_event(
        _clear_payload(
            edit_id,
            body="Provisional",
            room_id=room_id,
            edit_of=original_id,
            origin_server_ts=3000,
        ),
    )
    await event_cache.store_event(edit_id, room_id, provisional_edit)

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )

    assert latest is not None
    assert latest["event_id"] == edit_id
    assert latest["content"]["m.new_content"]["body"] == "Canonical"


@pytest.mark.asyncio
async def test_sequential_conflicting_clear_payloads_for_one_edit_fail_closed(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """A conflicting immutable identity stays quarantined across snapshots and restart."""
    room_id = "!room:localhost"
    original_id = "$original:localhost"
    edit_id = "$same-edit:localhost"

    def edit(body: str) -> dict[str, object]:
        return {
            "event_id": edit_id,
            "room_id": room_id,
            "sender": "@user:localhost",
            "origin_server_ts": 2000,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
            },
        }

    original = _clear_payload(original_id, body="Original")
    event_cache = event_cache_factory()
    await event_cache.initialize()
    try:
        first_edit = edit("First")
        first_edit["io.mindroom.provisional_outbound"] = True
        await event_cache.store_events_batch(
            [
                (original_id, room_id, original),
                (edit_id, room_id, first_edit),
            ],
        )
        original_with_bundle = json.loads(json.dumps(original))
        original_with_bundle["unsigned"] = {"m.relations": {"m.replace": edit("Second")}}
        await event_cache.store_events_batch(
            [
                (original_id, room_id, original_with_bundle),
                (edit_id, room_id, edit("Second")),
            ],
        )
        assert (
            await event_cache.get_latest_edit(
                room_id,
                original,
                validator=valid_room_message_replacement,
            )
            is None
        )
        assert await event_cache.get_event(room_id, edit_id) is None
        assert await event_cache.redacted_event_ids(room_id, {edit_id}) == {edit_id}
        cached_original = await event_cache.get_event(room_id, original_id)
        assert cached_original is not None
        assert "m.replace" not in cached_original["unsigned"]["m.relations"]

        await _replace_thread(
            event_cache,
            room_id,
            original_id,
            [original, edit("Snapshot retry")],
        )
        assert await event_cache.get_event(room_id, edit_id) is None
        assert await event_cache.get_thread_id_for_event(room_id, edit_id) is None
    finally:
        await event_cache.close()

    reopened_cache = event_cache_factory()
    await reopened_cache.initialize()
    try:
        await reopened_cache.store_event(edit_id, room_id, edit("Restart retry"))
        assert await reopened_cache.get_event(room_id, edit_id) is None
        assert (
            await reopened_cache.get_latest_edit(
                room_id,
                original,
                validator=valid_room_message_replacement,
            )
            is None
        )
    finally:
        await reopened_cache.close()


@pytest.mark.asyncio
async def test_same_event_identity_accepts_unsigned_metadata_refresh(
    event_cache: ConversationEventCache,
) -> None:
    """Mutable unsigned aggregation metadata may refresh one immutable event payload."""
    room_id = "!room:localhost"
    event_id = "$event:localhost"
    original = _clear_payload(event_id, body="Original")
    refreshed = json.loads(json.dumps(original))
    refreshed["unsigned"] = {"m.relations": {"m.thread": {"count": 2}}}

    await event_cache.store_event(event_id, room_id, original)
    await event_cache.store_event(event_id, room_id, refreshed)

    assert await event_cache.get_event(room_id, event_id) == refreshed
    assert await event_cache.redacted_event_ids(room_id, {event_id}) == set()


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
        assert await event_cache.append_event(room_id, thread_id, payloads[payload_kind])

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

    latest_edit = await get_latest_edit(
        event_cache,
        room_id,
        original_event_id,
        sender="@user:localhost",
        event_type="m.room.message",
    )
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
async def test_conflicting_clear_rewrite_quarantines_thread_index_row(
    event_cache: ConversationEventCache,
) -> None:
    """A conflicting clear rewrite must remove payload and derived membership."""
    room_id = "!room:localhost"
    event_id = "$moved:localhost"
    await event_cache.store_events_batch(
        [(event_id, room_id, _clear_payload(event_id, thread_root_id="$root-a:localhost"))],
    )
    assert await event_cache.get_thread_id_for_event(room_id, event_id) == "$root-a:localhost"

    await event_cache.store_events_batch(
        [(event_id, room_id, _clear_payload(event_id, thread_root_id="$root-b:localhost"))],
    )

    assert await event_cache.get_event(room_id, event_id) is None
    assert await event_cache.get_thread_id_for_event(room_id, event_id) is None
    assert await event_cache.redacted_event_ids(room_id, {event_id}) == {event_id}


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
async def test_cached_room_get_event_cache_hit_avoids_network_call(event_cache: ConversationEventCache) -> None:
    """Cached room get event lookups should reconstruct nio responses without I/O."""
    cache = event_cache

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
        response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Cached reply"
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_original_validation_precedes_edit_projection(
    event_cache: ConversationEventCache,
) -> None:
    """Point reads and snapshots must reject invalid originals before applying edits."""
    older_event = _make_text_event(
        event_id="$older",
        sender="@agent:localhost",
        body="Older",
        server_timestamp=1000,
        source_content={"body": "Older"},
    )
    cached_original = _make_text_event(
        event_id="$target",
        sender="@agent:localhost",
        body="Cached original",
        server_timestamp=2000,
        source_content={"body": "Cached original"},
    )
    cached_source = _cache_source(cached_original)
    cached_source["content"].pop("msgtype")
    edit = _make_text_event(
        event_id="$edit",
        sender="@agent:localhost",
        body="* Edited",
        server_timestamp=3000,
        source_content={
            "body": "* Edited",
            "m.new_content": {"body": "Edited", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        },
    )
    fetched_original = _make_text_event(
        event_id="$target",
        sender="@agent:localhost",
        body="Fetched original",
        server_timestamp=2000,
        source_content={"body": "Fetched original"},
    )
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(fetched_original))
    await event_cache.store_events_batch(
        [
            ("$older", "!room:localhost", _cache_source(older_event)),
            ("$target", "!room:localhost", cached_source),
            ("$edit", "!room:localhost", _cache_source(edit)),
        ],
    )

    response, fetched_source = await _cached_room_get_event(
        client,
        event_cache,
        "!room:localhost",
        "$target",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@agent:localhost",
        runtime_started_at=None,
    )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$target"
    assert response.event.body == "Edited"
    assert fetched_source is not None
    assert fetched_source["event_id"] == "$target"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$target")
    assert snapshot is not None
    assert snapshot.content["body"] == "Older"


@pytest.mark.asyncio
async def test_matrix_conversation_lookup_fill_cannot_cross_leave_and_rejoin(tmp_path: Path) -> None:
    """A point fetch begun before departure must not repopulate the rejoined cache."""
    db_path = tmp_path / "event_cache.db"
    principal_id = "@alice:localhost"
    room_id = "!room:localhost"
    event_id = "$lookup"
    lookup_root = SqliteEventCache(db_path)
    membership_root = SqliteEventCache(db_path)
    await lookup_root.initialize()
    await membership_root.initialize()
    lookup_cache = lookup_root.for_principal(principal_id)
    membership_cache = membership_root.for_principal(principal_id)
    event = _make_text_event(
        event_id=event_id,
        sender="@agent:localhost",
        body="Fetched",
        server_timestamp=1,
        source_content={"body": "Fetched"},
    )
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def room_get_event(_room_id: str, _event_id: str) -> MagicMock:
        fetch_started.set()
        await release_fetch.wait()
        return _make_room_get_event_response(event)

    client = MagicMock()
    client.room_get_event = AsyncMock(side_effect=room_get_event)
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, lookup_cache, client=client)
    conversation_cache.runtime.event_cache_write_coordinator = EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=conversation_cache.runtime,
    )
    lookup_task = asyncio.create_task(conversation_cache.get_event(room_id, event_id))
    try:
        await fetch_started.wait()
        departure_epoch = membership_cache.mark_room_departed(room_id)
        await membership_cache.purge_room(room_id)
        await membership_cache.mark_room_joined(
            room_id,
            expected_departure_epoch=departure_epoch,
        )
        release_fetch.set()

        response = await lookup_task
        assert isinstance(response, nio.RoomGetEventResponse)
        assert await lookup_cache.get_event(room_id, event_id) is None
    finally:
        release_fetch.set()
        if not lookup_task.done():
            await lookup_task
        await membership_root.close()
        await lookup_root.close()


@pytest.mark.asyncio
async def test_cached_room_get_event_cache_hit_returns_latest_visible_edit(
    event_cache: ConversationEventCache,
) -> None:
    """Point-event cache hits should surface the latest edited content for originals."""
    cache = event_cache

    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={
            "body": "Original reply",
            "formatted_body": "<p>Original reply</p>",
            "format": "org.matrix.custom.html",
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
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$other_thread"},
            },
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
        response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Final reply"
    assert response.event.server_timestamp == 2000
    assert EventInfo.from_event(response.event.source).thread_id == "$thread_root"
    assert response.event.source["content"] == {
        "body": "Final reply",
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
    }
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_room_get_event_ignores_foreign_sender_edit(
    event_cache: ConversationEventCache,
) -> None:
    """Cached reconstruction must enforce replacement sender ownership."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@alice:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply"},
    )
    forged_edit = _make_text_event(
        event_id="$forged_edit",
        sender="@mallory:localhost",
        body="* Forged reply",
        server_timestamp=3000,
        source_content={
            "body": "* Forged reply",
            "m.new_content": {"body": "Forged reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$forged_edit", "!room:localhost", _cache_source(forged_edit)),
        ],
    )

    response, _ = await _cached_room_get_event(client, event_cache, "!room:localhost", "$reply")

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original reply"
    assert response.event.server_timestamp == 2000
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_room_get_event_ignores_wrong_event_type_edit(
    event_cache: ConversationEventCache,
) -> None:
    """Cached reconstruction must require the replacement and original event types to match."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@alice:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply"},
    )
    wrong_type_edit = {
        "event_id": "$wrong_type_edit",
        "sender": "@alice:localhost",
        "origin_server_ts": 3000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "body": "* Wrong type",
            "m.new_content": {"body": "Wrong type"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    }
    client = MagicMock()
    client.room_get_event = AsyncMock()
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$wrong_type_edit", "!room:localhost", wrong_type_edit),
        ],
    )

    response, _ = await _cached_room_get_event(client, event_cache, "!room:localhost", "$reply")

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original reply"
    assert response.event.server_timestamp == 2000
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidity", ["missing-msgtype", "retargeted"])
async def test_cached_room_get_event_falls_back_from_invalid_hydrated_edit(
    event_cache: ConversationEventCache,
    invalidity: str,
) -> None:
    """SQLite and PostgreSQL point reads must skip a newest invalid hydrated sidecar."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@alice:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply", "msgtype": "m.text"},
    )
    valid_edit = _make_text_event(
        event_id="$valid_edit",
        sender="@alice:localhost",
        body="* Older valid",
        server_timestamp=3000,
        source_content={
            "body": "* Older valid",
            "msgtype": "m.text",
            "m.new_content": {"body": "Older valid", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    sidecar_edit = _make_text_event(
        event_id="$sidecar_edit",
        sender="@alice:localhost",
        body="* Preview",
        server_timestamp=4000,
        source_content={
            "body": "* Preview",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Preview",
                "msgtype": "m.file",
                "url": "mxc://server/invalid-edit",
                "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    client = AsyncMock(spec=nio.AsyncClient)
    canonical_new_content = {"body": "Invalid hydrated"}
    if invalidity == "retargeted":
        canonical_new_content["msgtype"] = "m.text"
    client.download.return_value = MagicMock(
        spec=nio.DownloadResponse,
        body=json.dumps(
            {
                "body": "* Invalid hydrated",
                "msgtype": "m.text",
                "m.new_content": canonical_new_content,
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$other" if invalidity == "retargeted" else "$reply",
                },
            },
        ).encode(),
    )
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$valid_edit", "!room:localhost", _cache_source(valid_edit)),
            ("$sidecar_edit", "!room:localhost", _cache_source(sidecar_edit)),
        ],
    )

    response, _ = await _cached_room_get_event(client, event_cache, "!room:localhost", "$reply")

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Older valid"
    assert response.event.server_timestamp == 2000
    client.download.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_new_content",
    [
        pytest.param(None, id="missing-new-content"),
        pytest.param({}, id="empty-new-content"),
    ],
)
async def test_cached_room_get_event_falls_back_from_malformed_newest_edit(
    event_cache: ConversationEventCache,
    malformed_new_content: dict[str, object] | None,
) -> None:
    """Cached reconstruction must retain an older valid edit when the newest edit is malformed."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@alice:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply"},
    )
    valid_edit = _make_text_event(
        event_id="$valid_edit",
        sender="@alice:localhost",
        body="* Good",
        server_timestamp=3000,
        source_content={
            "body": "* Good",
            "m.new_content": {"body": "Good", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    malformed_content: dict[str, object] = {
        "body": "* Malformed",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
    }
    if malformed_new_content is not None:
        malformed_content["m.new_content"] = malformed_new_content
    malformed_edit = _make_text_event(
        event_id="$malformed_edit",
        sender="@alice:localhost",
        body="* Malformed",
        server_timestamp=4000,
        source_content=malformed_content,
    )
    client = MagicMock()
    client.room_get_event = AsyncMock()
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$valid_edit", "!room:localhost", _cache_source(valid_edit)),
            ("$malformed_edit", "!room:localhost", _cache_source(malformed_edit)),
        ],
    )

    response, _ = await _cached_room_get_event(client, event_cache, "!room:localhost", "$reply")

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Good"
    assert response.event.server_timestamp == 2000
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_point_and_snapshot_reads_apply_bundled_replacement(
    event_cache: ConversationEventCache,
) -> None:
    """Cached projections must honor the same valid bundled edit as full history."""
    original_event = _cache_source(
        _make_text_event(
            event_id="$reply",
            sender="@alice:localhost",
            body="Original",
            server_timestamp=2000,
            source_content={"body": "Original", "msgtype": "m.text"},
        ),
    )
    bundled_edit = {
        "event_id": "$bundled_edit",
        "sender": "@alice:localhost",
        "origin_server_ts": 3000,
        "type": "m.room.message",
        "content": {
            "body": "* Bundled",
            "msgtype": "m.text",
            "m.new_content": {"body": "Bundled", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    }
    original_event["unsigned"] = {
        "m.relations": {
            "m.replace": bundled_edit,
        },
    }
    await _replace_thread(event_cache, "!room:localhost", "$thread", [original_event])

    response, _ = await _cached_room_get_event(
        AsyncMock(),
        event_cache,
        "!room:localhost",
        "$reply",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@alice:localhost",
        runtime_started_at=None,
    )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Bundled"
    assert response.event.server_timestamp == 2000
    assert snapshot is not None
    assert snapshot.content == {"body": "Bundled", "msgtype": "m.text"}
    assert snapshot.origin_server_ts == 2000

    await event_cache.store_event("$bundled_edit", "!room:localhost", bundled_edit)
    assert await event_cache.redact_event("!room:localhost", "$bundled_edit")
    cached_original = await event_cache.get_event("!room:localhost", "$reply")
    cached_thread = await event_cache.get_thread_events("!room:localhost", "$thread")
    assert cached_original is not None
    assert cached_thread == [cached_original]
    assert cached_original["unsigned"]["m.relations"].get("m.replace") is None
    assert (
        await event_cache.get_latest_edit(
            "!room:localhost",
            cached_original,
            validator=valid_room_message_replacement,
        )
        is None
    )

    response, _ = await _cached_room_get_event(AsyncMock(), event_cache, "!room:localhost", "$reply")
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@alice:localhost",
        runtime_started_at=None,
    )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original"
    assert snapshot is not None
    assert snapshot.content == {"body": "Original", "msgtype": "m.text"}

    late_original = json.loads(json.dumps(original_event))
    late_original["event_id"] = "$late_reply"
    late_edit = late_original["unsigned"]["m.relations"]["m.replace"]
    late_edit["event_id"] = "$late_bundled_edit"
    late_edit["content"]["m.relates_to"]["event_id"] = "$late_reply"
    assert not await event_cache.redact_event("!room:localhost", "$late_bundled_edit")
    await _replace_thread(event_cache, "!room:localhost", "$late_reply", [late_original])
    cached_late_original = await event_cache.get_event("!room:localhost", "$late_reply")
    cached_late_thread = await event_cache.get_thread_events("!room:localhost", "$late_reply")
    assert cached_late_original is not None
    assert cached_late_original["unsigned"]["m.relations"].get("m.replace") is None
    assert cached_late_thread == [cached_late_original]
    assert (
        await event_cache.get_latest_edit(
            "!room:localhost",
            cached_late_original,
            validator=valid_room_message_replacement,
        )
        is None
    )
    response, _ = await _cached_room_get_event(AsyncMock(), event_cache, "!room:localhost", "$late_reply")
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original"


@pytest.mark.asyncio
async def test_redacting_bundled_replacement_preserves_older_candidate(
    event_cache: ConversationEventCache,
) -> None:
    """Redacting one bundled edit must expose the next Matrix-valid candidate."""
    original_event = _cache_source(
        _make_text_event(
            event_id="$reply",
            sender="@alice:localhost",
            body="Original",
            server_timestamp=2000,
            source_content={"body": "Original", "msgtype": "m.text"},
        ),
    )

    def bundled_edit(event_id: str, body: str, timestamp: int) -> dict[str, object]:
        return {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
            },
        }

    older_edit = bundled_edit("$older_edit", "Older", 3000)
    newest_edit = bundled_edit("$newest_edit", "Newest", 4000)
    newest_edit["event"] = older_edit
    original_event["unsigned"] = {"m.relations": {"m.replace": newest_edit}}
    await _replace_thread(event_cache, "!room:localhost", "$thread", [original_event])
    await event_cache.store_event("$newest_edit", "!room:localhost", newest_edit)

    assert await event_cache.redact_event("!room:localhost", "$newest_edit")
    cached_original = await event_cache.get_event("!room:localhost", "$reply")
    assert cached_original is not None
    bundled = cached_original["unsigned"]["m.relations"]["m.replace"]
    assert bundled["event"]["event_id"] == "$older_edit"
    latest = await event_cache.get_latest_edit(
        "!room:localhost",
        cached_original,
        validator=valid_room_message_replacement,
    )
    assert latest is not None
    assert latest["event_id"] == "$older_edit"

    response, _ = await _cached_room_get_event(AsyncMock(), event_cache, "!room:localhost", "$reply")
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Older"


@pytest.mark.asyncio
async def test_redacting_only_nested_bundled_edit_does_not_promote_wrapper(
    event_cache: ConversationEventCache,
) -> None:
    """Removing the canonical nested edit must not turn aggregation metadata into an edit."""
    room_id = "!room:localhost"
    original_id = "$reply"

    def edit(event_id: str, body: str, timestamp: int) -> dict[str, object]:
        return {
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": timestamp,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
            },
        }

    original = _clear_payload(original_id, body="Original")
    wrapper = edit("$wrapper", "Wrapper metadata", 3000)
    wrapper["latest_event"] = edit("$nested", "Nested canonical", 2000)
    original["unsigned"] = {"m.relations": {"m.replace": wrapper}}
    await _replace_thread(event_cache, room_id, original_id, [original])

    latest = await event_cache.get_latest_edit(
        room_id,
        original,
        validator=valid_room_message_replacement,
    )
    assert latest is not None
    assert latest["event_id"] == "$nested"

    assert await event_cache.redact_event(room_id, "$nested")
    cached_original = await event_cache.get_event(room_id, original_id)
    assert cached_original is not None
    assert (
        await event_cache.get_latest_edit(
            room_id,
            cached_original,
            validator=valid_room_message_replacement,
        )
        is None
    )


@pytest.mark.asyncio
async def test_thread_append_sanitizes_tombstoned_bundled_replacement(
    event_cache: ConversationEventCache,
) -> None:
    """Incremental append must persist the filtered original, not its tombstoned bundle."""
    room_id = "!room:localhost"
    thread_id = "$thread:localhost"
    original_event_id = "$late_reply"
    edit_event_id = "$late_edit"
    await _replace_thread(
        event_cache,
        room_id,
        thread_id,
        [_clear_payload(thread_id, body="root")],
    )
    original_event = _clear_payload(
        original_event_id,
        body="Original",
        thread_root_id=thread_id,
        origin_server_ts=2000,
    )
    original_event["unsigned"] = {
        "m.relations": {
            "m.replace": {
                "event_id": edit_event_id,
                "sender": original_event["sender"],
                "origin_server_ts": 3000,
                "type": "m.room.message",
                "content": {
                    "body": "* Forged",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "Forged", "msgtype": "m.text"},
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": original_event_id,
                    },
                },
            },
        },
    }

    assert not await event_cache.redact_event(room_id, edit_event_id)
    assert await event_cache.append_event(room_id, thread_id, original_event)

    cached_original = await event_cache.get_event(room_id, original_event_id)
    cached_thread = await event_cache.get_thread_events(room_id, thread_id)
    assert cached_original is not None
    assert cached_original["unsigned"]["m.relations"].get("m.replace") is None
    assert cached_thread is not None
    assert next(event for event in cached_thread if event["event_id"] == original_event_id) == cached_original
    assert (
        await event_cache.get_latest_edit(
            room_id,
            cached_original,
            validator=valid_room_message_replacement,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_scope", "point_cached"),
    [({"state_key": ""}, True), ({"room_id": "!other:localhost"}, False)],
    ids=["state", "wrong-room"],
)
async def test_invalid_relation_events_do_not_create_thread_or_edit_indexes(
    event_cache: ConversationEventCache,
    invalid_scope: dict[str, str],
    point_cached: bool,
) -> None:
    """State events stay point-only, while explicit wrong-room events are not cached."""
    original = _cache_source(
        _make_text_event(
            event_id="$original",
            sender="@alice:localhost",
            body="Original",
            server_timestamp=1000,
            source_content={"body": "Original", "msgtype": "m.text"},
        ),
    )
    invalid_reply = _cache_source(
        _make_text_event(
            event_id="$invalid_reply",
            sender="@alice:localhost",
            body="Reply",
            server_timestamp=2000,
            source_content={
                "body": "Reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
            },
        ),
    )
    invalid_edit = _cache_source(
        _make_text_event(
            event_id="$invalid_edit",
            sender="@alice:localhost",
            body="* Edited",
            server_timestamp=3000,
            source_content={
                "body": "* Edited",
                "msgtype": "m.text",
                "m.new_content": {"body": "Edited", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        ),
    )
    invalid_reply.update(invalid_scope)
    invalid_edit.update(invalid_scope)
    await event_cache.store_events_batch(
        [
            ("$original", "!room:localhost", original),
            ("$invalid_reply", "!room:localhost", invalid_reply),
            ("$invalid_edit", "!room:localhost", invalid_edit),
        ],
    )

    assert await event_cache.get_thread_id_for_event("!room:localhost", "$invalid_reply") is None
    assert (await event_cache.get_event("!room:localhost", "$invalid_reply") is not None) is point_cached
    assert await event_cache.redact_event("!room:localhost", "$original")
    replacement_point_id = "$replacement_point"
    replacement_point = {
        "event_id": replacement_point_id,
        "sender": "@alice:localhost",
        "origin_server_ts": 4000,
        "type": "m.room.message",
        "content": {"body": "Independent", "msgtype": "m.text"},
    }
    await event_cache.store_event(replacement_point_id, "!room:localhost", replacement_point)
    assert await event_cache.get_event("!room:localhost", replacement_point_id) == replacement_point


@pytest.mark.asyncio
async def test_orphan_edit_does_not_create_thread_index_from_replacement_content(
    event_cache: ConversationEventCache,
) -> None:
    """A point-cached edit cannot establish thread membership without its original."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    await event_cache.store_event(
        "$orphan_edit",
        room_id,
        {
            "event_id": "$orphan_edit",
            "room_id": room_id,
            "sender": "@mallory:localhost",
            "type": "m.room.message",
            "origin_server_ts": 2000,
            "content": {
                "body": "* forged",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "forged",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$missing"},
            },
        },
    )

    assert await event_cache.get_thread_id_for_event(room_id, "$orphan_edit") is None
    assert await event_cache.get_thread_id_for_event(room_id, thread_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidity",
    [
        "missing-sender",
        "wrong-type",
        "missing-body",
        "missing-msgtype",
        "missing-media-transport",
        "malformed-plain-media-url",
        "malformed-encrypted-file",
        "wrong-target",
    ],
)
async def test_cached_edit_paths_fall_back_from_invalid_newest_event_envelope(
    event_cache: ConversationEventCache,
    invalidity: str,
) -> None:
    """Direct lookup, point reads, and snapshots must skip malformed cached event envelopes."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@alice:localhost",
        body="Original",
        server_timestamp=2000,
        source_content={"body": "Original", "msgtype": "m.text"},
    )
    older_edit = _make_text_event(
        event_id="$older_edit",
        sender="@alice:localhost",
        body="* Older valid",
        server_timestamp=3000,
        source_content={
            "body": "* Older valid",
            "m.new_content": {"body": "Older valid", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    newest_edit = _make_text_event(
        event_id="$newest_edit",
        sender="@alice:localhost",
        body="* Invalid newest",
        server_timestamp=4000,
        source_content={
            "body": "* Invalid newest",
            "m.new_content": {"body": "Invalid newest", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    newest_source = _cache_source(newest_edit)
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$older_edit", "!room:localhost", _cache_source(older_edit)),
            ("$newest_edit", "!room:localhost", newest_source),
        ],
    )
    malformed_source = json.loads(json.dumps(newest_source))
    malformed_content = malformed_source["content"]
    if invalidity == "missing-sender":
        malformed_source.pop("sender")
    elif invalidity == "wrong-type":
        malformed_source["type"] = "io.mindroom.tool_approval"
    elif invalidity == "missing-body":
        malformed_content.pop("body")
    elif invalidity == "missing-msgtype":
        malformed_content.pop("msgtype")
    elif invalidity == "missing-media-transport":
        malformed_content["msgtype"] = "m.image"
        malformed_content["m.new_content"]["msgtype"] = "m.image"
    elif invalidity == "malformed-plain-media-url":
        malformed_content["msgtype"] = "m.image"
        malformed_content["url"] = "not-an-mxc-uri"
        malformed_content["m.new_content"] = {
            "body": "Invalid newest",
            "msgtype": "m.image",
            "url": "not-an-mxc-uri",
        }
    elif invalidity == "malformed-encrypted-file":
        malformed_content["msgtype"] = "m.image"
        malformed_content["file"] = {
            "url": "not-an-mxc-uri",
            "v": "v2",
            "key": {"alg": "wrong", "k": "not-base64"},
            "iv": "not-base64",
            "hashes": {},
        }
        malformed_content["m.new_content"] = {
            "body": "Invalid newest",
            "msgtype": "m.image",
            "file": malformed_content["file"],
        }
    else:
        malformed_content["m.relates_to"]["event_id"] = "$different"
    await event_cache.store_event(
        "$newest_edit",
        "!room:localhost",
        malformed_source,
    )

    latest_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$reply",
        sender="@alice:localhost",
        event_type="m.room.message",
    )
    response, _ = await _cached_room_get_event(
        AsyncMock(),
        event_cache,
        "!room:localhost",
        "$reply",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@alice:localhost",
        runtime_started_at=None,
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$older_edit"
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Older valid"
    assert response.event.server_timestamp == 2000
    assert snapshot is not None
    assert snapshot.content["body"] == "Older valid"
    assert snapshot.origin_server_ts == 2000


@pytest.mark.asyncio
async def test_latest_agent_message_snapshot_falls_back_from_empty_new_content(
    event_cache: ConversationEventCache,
) -> None:
    """Cached snapshots must ignore an unrenderable newest message replacement."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={
            "body": "Original reply",
            "formatted_body": "<p>Original reply</p>",
            "format": "org.matrix.custom.html",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$question"}},
        },
    )
    valid_edit = _make_text_event(
        event_id="$valid_edit",
        sender="@agent:localhost",
        body="* Good",
        server_timestamp=3000,
        source_content={
            "body": "* Good",
            "m.new_content": {
                "body": "Good",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$other_thread",
                },
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    malformed_edit = _make_text_event(
        event_id="$malformed_edit",
        sender="@agent:localhost",
        body="* Malformed",
        server_timestamp=4000,
        source_content={
            "body": "* Malformed",
            "m.new_content": {},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$valid_edit", "!room:localhost", _cache_source(valid_edit)),
            ("$malformed_edit", "!room:localhost", _cache_source(malformed_edit)),
        ],
    )

    latest_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$reply",
        sender="@agent:localhost",
        event_type="m.room.message",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@agent:localhost",
        runtime_started_at=None,
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$valid_edit"
    assert snapshot is not None
    assert snapshot.content == {
        "body": "Good",
        "msgtype": "m.text",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$question"}},
    }
    assert snapshot.origin_server_ts == 2000


@pytest.mark.asyncio
async def test_cached_edit_paths_ignore_explicit_other_room(
    event_cache: ConversationEventCache,
) -> None:
    """Cached edit lookup, point projection, and snapshots must enforce the caller room."""
    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply", "msgtype": "m.text"},
    )
    valid_edit = _make_text_event(
        event_id="$valid_edit",
        sender="@agent:localhost",
        body="* Good",
        server_timestamp=3000,
        source_content={
            "body": "* Good",
            "m.new_content": {"body": "Good", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    wrong_room_edit = _make_text_event(
        event_id="$wrong_room_edit",
        sender="@agent:localhost",
        body="* Wrong room",
        server_timestamp=4000,
        source_content={
            "body": "* Wrong room",
            "m.new_content": {"body": "Wrong room", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    )
    wrong_room_source = _cache_source(wrong_room_edit)
    wrong_room_source["room_id"] = "!other:localhost"
    await event_cache.store_events_batch(
        [
            ("$reply", "!room:localhost", _cache_source(original_event)),
            ("$valid_edit", "!room:localhost", _cache_source(valid_edit)),
            ("$wrong_room_edit", "!room:localhost", wrong_room_source),
        ],
    )

    latest_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$reply",
        sender="@agent:localhost",
        event_type="m.room.message",
    )
    response, _ = await _cached_room_get_event(
        AsyncMock(),
        event_cache,
        "!room:localhost",
        "$reply",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@agent:localhost",
        runtime_started_at=None,
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$valid_edit"
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Good"
    assert response.event.server_timestamp == 2000
    assert snapshot is not None
    assert snapshot.content["body"] == "Good"
    assert snapshot.origin_server_ts == 2000


@pytest.mark.asyncio
async def test_cached_state_message_is_not_edited_or_returned_as_snapshot(
    event_cache: ConversationEventCache,
) -> None:
    """Cached state messages must not be projected as visible edited messages."""
    state_original = {
        "event_id": "$state",
        "sender": "@agent:localhost",
        "origin_server_ts": 2000,
        "type": "m.room.message",
        "state_key": "",
        "content": {"msgtype": "m.text", "body": "State"},
    }
    edit = {
        "event_id": "$edit",
        "sender": "@agent:localhost",
        "origin_server_ts": 3000,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": "* Edited state",
            "m.new_content": {"msgtype": "m.text", "body": "Edited state"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$state"},
        },
    }
    await event_cache.store_events_batch(
        [
            ("$state", "!room:localhost", state_original),
            ("$edit", "!room:localhost", edit),
        ],
    )

    response, _ = await _cached_room_get_event(
        AsyncMock(),
        event_cache,
        "!room:localhost",
        "$state",
    )
    snapshot = await event_cache.get_latest_agent_message_snapshot(
        "!room:localhost",
        None,
        "@agent:localhost",
        runtime_started_at=None,
    )

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.source["content"]["body"] == "State"
    assert snapshot is None


@pytest.mark.asyncio
async def test_custom_edit_lookup_ignores_explicit_other_room(
    event_cache: ConversationEventCache,
) -> None:
    """Custom approval edit lookup must enforce explicit room evidence too."""
    valid_edit = {
        "event_id": "$valid_edit",
        "sender": "@bot:localhost",
        "origin_server_ts": 2000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "m.new_content": {"status": "approved"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }
    wrong_room_edit = {
        "event_id": "$wrong_room_edit",
        "room_id": "!other:localhost",
        "sender": "@bot:localhost",
        "origin_server_ts": 3000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "m.new_content": {"status": "denied"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }
    await event_cache.store_events_batch(
        [
            ("$valid_edit", "!room:localhost", valid_edit),
            ("$wrong_room_edit", "!room:localhost", wrong_room_edit),
        ],
    )

    latest_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$approval",
        sender="@bot:localhost",
        event_type="io.mindroom.tool_approval",
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$valid_edit"


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
    approval_card["unsigned"] = {"m.relations": {"m.replace": approval_edit}}

    try:
        await cache.store_events_batch(
            [
                ("$approval", "!room:localhost", approval_card),
                ("$approval_edit", "!room:localhost", approval_edit),
            ],
        )
        latest_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$approval",
            sender="@bot:localhost",
            event_type="io.mindroom.tool_approval",
        )
        assert await cache.redact_event("!room:localhost", "$approval_edit")
        cached_card = await cache.get_event("!room:localhost", "$approval")
        latest_after_redaction = await get_latest_edit(
            cache,
            "!room:localhost",
            "$approval",
            sender="@bot:localhost",
            event_type="io.mindroom.tool_approval",
        )
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$approval_edit"
    assert latest_edit["content"]["m.new_content"]["status"] == "approved"
    assert cached_card is not None
    assert cached_card["content"]["status"] == "pending"
    assert cached_card["unsigned"]["m.relations"].get("m.replace") is None
    assert latest_after_redaction is None


@pytest.mark.asyncio
async def test_approval_edit_lookup_falls_back_from_invalid_newest_status(
    event_cache: ConversationEventCache,
) -> None:
    """Approval lookup must retain an older terminal edit when the newest status is invalid."""
    valid_edit = {
        "event_id": "$valid_edit",
        "sender": "@bot:localhost",
        "origin_server_ts": 2000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "m.new_content": {"status": "approved"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }
    malformed_edit = {
        "event_id": "$malformed_edit",
        "sender": "@bot:localhost",
        "origin_server_ts": 3000,
        "type": "io.mindroom.tool_approval",
        "content": {
            "m.new_content": {},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval"},
        },
    }
    await event_cache.store_events_batch(
        [
            ("$valid_edit", "!room:localhost", valid_edit),
            ("$malformed_edit", "!room:localhost", malformed_edit),
        ],
    )

    latest_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$approval",
        sender="@bot:localhost",
        event_type="io.mindroom.tool_approval",
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$valid_edit"


@pytest.mark.asyncio
async def test_latest_edit_equal_timestamp_uses_greatest_event_id(
    event_cache: ConversationEventCache,
) -> None:
    """Equal-timestamp replacements must use Matrix event-ID ordering, not cache write order."""
    room_id = "!room:localhost"
    original_event_id = "$original"
    uppercase_edit = _make_text_event(
        event_id="$Z-edit",
        sender="@alice:localhost",
        body="* Uppercase",
        server_timestamp=2000,
        source_content={
            "body": "* Uppercase",
            "m.new_content": {"body": "Uppercase", "msgtype": "m.text"},
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
        },
    )
    lowercase_edit = _make_text_event(
        event_id="$a-edit",
        sender="@alice:localhost",
        body="* Lowercase",
        server_timestamp=2000,
        source_content={
            "body": "* Lowercase",
            "m.new_content": {"body": "Lowercase", "msgtype": "m.text"},
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_event_id,
            },
        },
    )

    await event_cache.store_event(lowercase_edit.event_id, room_id, _cache_source(lowercase_edit))
    await event_cache.store_event(uppercase_edit.event_id, room_id, _cache_source(uppercase_edit))

    latest_edit = await get_latest_edit(
        event_cache,
        room_id,
        original_event_id,
        sender="@alice:localhost",
        event_type="m.room.message",
    )

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$a-edit"


@pytest.mark.asyncio
async def test_latest_edit_excludes_candidates_the_caller_already_rejected(
    event_cache: ConversationEventCache,
) -> None:
    """Excluding an unresolvable newest replacement must surface the next-newest valid one.

    A replacement can pass identity and envelope validation and still fail to resolve, for
    example when its sidecar cannot be hydrated. Callers exclude it and ask again, so a broken
    newest edit must never hide an older valid edit.
    """
    room_id = "!room:localhost"
    original_event_id = "$original"

    def edit(event_id: str, body: str, timestamp: int) -> nio.RoomMessageText:
        return _make_text_event(
            event_id=event_id,
            sender="@alice:localhost",
            body=f"* {body}",
            server_timestamp=timestamp,
            source_content={
                "body": f"* {body}",
                "m.new_content": {"body": body, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
            },
        )

    older_edit = edit("$older-edit", "Older", 2000)
    newest_edit = edit("$newest-edit", "Newest", 3000)
    await event_cache.store_event(older_edit.event_id, room_id, _cache_source(older_edit))
    await event_cache.store_event(newest_edit.event_id, room_id, _cache_source(newest_edit))

    unfiltered = await get_latest_edit(
        event_cache,
        room_id,
        original_event_id,
        sender="@alice:localhost",
        event_type="m.room.message",
    )
    assert unfiltered is not None
    assert unfiltered["event_id"] == "$newest-edit"

    fallback = await get_latest_edit(
        event_cache,
        room_id,
        original_event_id,
        sender="@alice:localhost",
        event_type="m.room.message",
        excluded_event_ids={"$newest-edit"},
    )
    assert fallback is not None
    assert fallback["event_id"] == "$older-edit"

    assert (
        await get_latest_edit(
            event_cache,
            room_id,
            original_event_id,
            sender="@alice:localhost",
            event_type="m.room.message",
            excluded_event_ids={"$newest-edit", "$older-edit"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_latest_edit_requires_sender_scope_when_newer_edit_is_untrusted(
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
        latest_untrusted_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$approval",
            sender="@attacker:localhost",
            event_type="io.mindroom.tool_approval",
        )
        latest_trusted_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$approval",
            sender="@bot:localhost",
            event_type="io.mindroom.tool_approval",
        )
    finally:
        await cache.close()

    assert latest_untrusted_edit is not None
    assert latest_untrusted_edit["event_id"] == "$untrusted_edit"
    assert latest_trusted_edit is not None
    assert latest_trusted_edit["event_id"] == "$trusted_edit"


@pytest.mark.asyncio
async def test_cached_room_get_event_network_fetch_merges_cached_latest_edit(
    event_cache: ConversationEventCache,
) -> None:
    """Network fetches should still project originals through cached latest edits."""
    cache = event_cache

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
        response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.event_id == "$reply"
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


@pytest.mark.asyncio
async def test_cached_room_get_event_first_fetch_rejects_cached_cross_target_bundle(
    event_cache: ConversationEventCache,
) -> None:
    """A first network point read must not expose a conflicting bundled edit once."""
    room_id = "!room:localhost"
    original_id = "$reply"
    conflict_id = "$reply_edit"
    original_event = _make_text_event_with_bundled_edit(
        room_id=room_id,
        event_id=original_id,
        sender="@agent:localhost",
        body="Original reply",
        edit_id=conflict_id,
        edited_body="Bundled forged",
    )
    cached_conflict = _clear_payload(
        conflict_id,
        body="Cached other target",
        sender="@agent:localhost",
        room_id=room_id,
        edit_of="$other",
        origin_server_ts=3000,
    )
    await event_cache.store_event(conflict_id, room_id, cached_conflict)
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(original_event))

    response, fetched_source = await _cached_room_get_event(client, event_cache, room_id, original_id)

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original reply"
    assert fetched_source == original_event.source
    assert await event_cache.get_event(room_id, original_id) is None


@pytest.mark.asyncio
async def test_thread_root_preview_rejects_cached_cross_target_bundled_identity(
    tmp_path: Path,
    event_cache: ConversationEventCache,
) -> None:
    """A bundled preview must compare its edit identity with cached point payloads."""
    room_id = "!room:localhost"
    original_id = "$root"
    conflict_id = "$root_edit"
    original_event = _make_text_event_with_bundled_edit(
        room_id=room_id,
        event_id=original_id,
        sender="@agent:localhost",
        body="Original root",
        edit_id=conflict_id,
        edited_body="Bundled forged",
    )
    cached_conflict = _clear_payload(
        conflict_id,
        body="Cached other target",
        sender="@agent:localhost",
        room_id=room_id,
        edit_of="$other",
        origin_server_ts=3000,
    )
    await event_cache.store_event(conflict_id, room_id, cached_conflict)
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    preview = await thread_root_body_preview(
        original_event,
        client=client,
        config=conversation_cache.runtime.config,
        runtime_paths=conversation_cache.runtime.runtime_paths,
        event_cache=event_cache,
        room_id=room_id,
    )

    assert preview == "Original root"


@pytest.mark.asyncio
async def test_thread_root_preview_accepts_cached_encrypted_edit_representation(
    tmp_path: Path,
    event_cache: ConversationEventCache,
) -> None:
    """A cached ciphertext view must not hide its clear bundled preview."""
    room_id = "!room:localhost"
    original_id = "$root"
    edit_id = "$root_edit"
    original_event = _make_text_event_with_bundled_edit(
        room_id=room_id,
        event_id=original_id,
        sender="@user:localhost",
        body="Original root",
        edit_id=edit_id,
        edited_body="Bundled clear",
    )
    encrypted_edit = _opaque_payload(edit_id, origin_server_ts=3000)
    encrypted_edit["room_id"] = room_id
    await event_cache.store_event(edit_id, room_id, encrypted_edit)
    client = MagicMock()
    conversation_cache = _conversation_cache_for_thread_reads(tmp_path, event_cache, client=client)

    preview = await thread_root_body_preview(
        original_event,
        client=client,
        config=conversation_cache.runtime.config,
        runtime_paths=conversation_cache.runtime.runtime_paths,
        event_cache=event_cache,
        room_id=room_id,
    )

    assert preview == "Bundled clear"


@pytest.mark.asyncio
async def test_cached_room_get_event_network_fetch_ignores_tombstoned_bundled_edit(
    event_cache: ConversationEventCache,
) -> None:
    """A cache-miss point read must not resurrect a durably redacted bundled edit."""
    room_id = "!room:localhost"
    original_event = _make_text_event(
        event_id="$reply",
        sender="@agent:localhost",
        body="Original reply",
        server_timestamp=2000,
        source_content={"body": "Original reply"},
    )
    bundled_edit = {
        "event_id": "$reply_edit",
        "sender": "@agent:localhost",
        "origin_server_ts": 3000,
        "type": "m.room.message",
        "content": {
            "body": "* Redacted reply",
            "msgtype": "m.text",
            "m.new_content": {"body": "Redacted reply", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
        },
    }
    original_event.source["unsigned"] = {"m.relations": {"m.replace": bundled_edit}}
    client = MagicMock()
    client.room_get_event = AsyncMock(return_value=_make_room_get_event_response(original_event))

    assert not await event_cache.redact_event(room_id, "$reply_edit")
    response, _ = await _cached_room_get_event(client, event_cache, room_id, "$reply")

    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Original reply"


@pytest.mark.asyncio
async def test_redacting_latest_edit_falls_back_to_previous_cached_edit(event_cache: ConversationEventCache) -> None:
    """Removing the newest edit should expose the previous cached visible state."""
    cache = event_cache

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
        latest_response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
        redacted = await cache.redact_event("!room:localhost", "$reply_edit_2")
        fallback_response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert redacted is True
    assert isinstance(latest_response, nio.RoomGetEventResponse)
    assert latest_response.event.body == "Final reply"
    assert isinstance(fallback_response, nio.RoomGetEventResponse)
    assert fallback_response.event.body == "Intermediate reply"
    client.room_get_event.assert_not_awaited()


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
async def test_redacting_original_removes_dependent_cached_edits_from_thread_history(
    event_cache: ConversationEventCache,
) -> None:
    """Redacting an original must also remove cached edits that would resurrect it."""
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
        latest_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$reply",
            sender="@agent:localhost",
            event_type="m.room.message",
        )
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

        latest_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$reply",
            sender="@agent:localhost",
            event_type="m.room.message",
        )
        response, _ = await _cached_room_get_event(client, cache, "!room:localhost", "$reply")
    finally:
        await cache.close()

    assert latest_edit is not None
    assert latest_edit["event_id"] == "$reply_edit"
    assert isinstance(response, nio.RoomGetEventResponse)
    assert response.event.body == "Final reply"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply")


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
async def test_redaction_removes_event_thread_rows_and_blocks_late_edit_resurrection(
    event_cache: ConversationEventCache,
) -> None:
    """Redacting a reply must clear durable thread mapping and ignore late edits for that reply."""
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
async def test_store_event_does_not_index_malformed_thread_relation(
    event_cache: ConversationEventCache,
) -> None:
    """A stored malformed room message cannot create event or root thread indexes."""
    room_id = "!room:localhost"
    malformed_event = {
        "event_id": "$malformed",
        "sender": "@mallory:localhost",
        "origin_server_ts": 2000,
        "room_id": room_id,
        "type": "m.room.message",
        "content": {
            "body": "forged",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$victim_thread",
            },
        },
    }

    await event_cache.store_event("$malformed", room_id, malformed_event)

    assert await event_cache.get_event(room_id, "$malformed") is not None
    assert await event_cache.get_thread_id_for_event(room_id, "$malformed") is None
    assert await event_cache.get_thread_id_for_event(room_id, "$victim_thread") is None


@pytest.mark.asyncio
async def test_store_event_prevents_payload_id_from_retargeting_existing_indexes(
    event_cache: ConversationEventCache,
) -> None:
    """A contradictory payload ID cannot overwrite another event's edit index."""
    legitimate_edit = {
        "event_id": "$legitimate",
        "sender": "@agent:localhost",
        "origin_server_ts": 2000,
        "type": "m.room.message",
        "content": {
            "body": "* Legitimate",
            "msgtype": "m.text",
            "m.new_content": {"body": "Legitimate", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
    }
    contradictory_payload = {
        **legitimate_edit,
        "content": {
            **legitimate_edit["content"],
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$other"},
        },
    }

    await event_cache.store_event("$legitimate", "!room:localhost", legitimate_edit)
    await event_cache.store_event("$poison", "!room:localhost", contradictory_payload)

    original_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$original",
        sender="@agent:localhost",
        event_type="m.room.message",
    )
    other_edit = await get_latest_edit(
        event_cache,
        "!room:localhost",
        "$other",
        sender="@agent:localhost",
        event_type="m.room.message",
    )

    assert original_edit is not None
    assert original_edit["event_id"] == "$legitimate"
    assert other_edit is not None
    assert other_edit["event_id"] == "$poison"


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
        latest_edit = await get_latest_edit(
            cache,
            "!room:localhost",
            "$reply",
            sender="@agent:localhost",
            event_type="m.room.message",
        )
        cached_original = await cache.get_event("!room:localhost", "$reply")
    finally:
        await cache.close()

    with closing(sqlite3.connect(db_path)) as db:
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]

    assert latest_edit is None
    assert cached_original is None
    assert schema_version == event_cache_module._EVENT_CACHE_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_disabled_event_cache_skips_latest_agent_message_snapshot_reads(
    event_cache: ConversationEventCache,
) -> None:
    """Disabled caches should fail open for latest-agent-message snapshot reads."""
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
async def test_fetch_thread_history_cache_hit_avoids_full_fetch_calls(event_cache: ConversationEventCache) -> None:
    """Cache hits should bypass the full root-plus-relations fetch path."""
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
async def test_fetch_thread_history_cache_miss_does_full_fetch(event_cache: ConversationEventCache) -> None:
    """Cache misses should scan room history and populate the cache."""
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


@pytest.mark.asyncio
@pytest.mark.parametrize("removal", ["redaction", "identity-conflict"])
async def test_bundled_replacement_removal_advances_thread_point_revision(
    event_cache: ConversationEventCache,
    removal: str,
) -> None:
    """Scrubbing a bundled edit must invalidate process-local resolved-history reuse."""
    room_id = "!room:localhost"
    thread_id = "$thread_root"
    edit_id = "$edit"
    root = _clear_payload(
        thread_id,
        body="Root",
        room_id=room_id,
        origin_server_ts=1000,
    )
    root["unsigned"] = {
        "m.relations": {
            "m.replace": _clear_payload(
                edit_id,
                body="Bundled",
                room_id=room_id,
                edit_of=thread_id,
                origin_server_ts=2000,
            ),
        },
    }
    await _replace_thread(event_cache, room_id, thread_id, [root])
    before = await event_cache.get_thread_revision(room_id, thread_id)

    if removal == "redaction":
        assert await event_cache.redact_event(room_id, edit_id)
    else:
        await event_cache.store_event(
            edit_id,
            room_id,
            _clear_payload(
                edit_id,
                body="Conflicting explicit",
                room_id=room_id,
                edit_of=thread_id,
                origin_server_ts=2000,
            ),
        )

    after = await event_cache.get_thread_revision(room_id, thread_id)
    cached_root = await event_cache.get_event(room_id, thread_id)

    assert before is not None
    assert after is not None
    assert after.event_count == before.event_count
    assert after.max_write_seq > before.max_write_seq
    assert cached_root is not None
    assert "m.replace" not in cached_root["unsigned"]["m.relations"]


@pytest.mark.asyncio
async def test_thread_revision_tracks_point_payload_updates(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Thread revisions and delta reads include accepted encrypted-to-clear upgrades."""
    cache = event_cache_factory()
    await cache.initialize()
    root = _opaque_payload(
        "$thread_root",
        thread_root_id="$thread_root",
        origin_server_ts=1000,
    )
    try:
        await _replace_thread(cache, "!room:localhost", "$thread_root", [root])
        before = await cache.get_thread_revision("!room:localhost", "$thread_root")
        updated = _clear_payload(
            "$thread_root",
            body="updated",
            thread_root_id="$thread_root",
            origin_server_ts=1000,
        )
        await cache.store_event("$thread_root", "!room:localhost", updated)
        after = await cache.get_thread_revision("!room:localhost", "$thread_root")
        assert before is not None
        assert after is not None
        changed_rows = await cache.get_thread_events_written_between(
            "!room:localhost",
            "$thread_root",
            after_write_seq=before.max_write_seq,
            through_write_seq=after.max_write_seq,
            after_thread_write_seq=before.max_thread_write_seq,
            through_thread_write_seq=after.max_thread_write_seq,
        )
    finally:
        await cache.close()

    assert before.event_count == after.event_count == 1
    assert after.max_write_seq > before.max_write_seq
    assert after.max_thread_write_seq == before.max_thread_write_seq
    assert changed_rows == [updated]


@pytest.mark.asyncio
async def test_thread_revision_tracks_index_replacements(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Thread revisions detect membership changes when payload writes are refused."""
    cache = event_cache_factory()
    await cache.initialize()
    room_id = "!room:localhost"
    thread_id = "$thread_root"
    root = _clear_payload(thread_id, body="root", origin_server_ts=1000)
    old_reply = _clear_payload(
        "$old",
        body="old",
        thread_root_id=thread_id,
        origin_server_ts=2000,
    )
    new_reply = _clear_payload(
        "$new",
        body="new",
        thread_root_id=thread_id,
        origin_server_ts=2000,
    )
    shared_reply = _clear_payload(
        "$shared",
        body="shared",
        thread_root_id=thread_id,
        origin_server_ts=3000,
    )
    try:
        await cache.store_event("$new", room_id, new_reply)
        await _replace_thread(cache, room_id, thread_id, [root, old_reply, shared_reply])
        before = await cache.get_thread_revision(room_id, thread_id)
        replacement = [
            _opaque_payload(thread_id, origin_server_ts=1000),
            _opaque_payload("$new", thread_root_id=thread_id, origin_server_ts=2000),
            _opaque_payload("$shared", thread_root_id=thread_id, origin_server_ts=3000),
        ]
        await _replace_thread(cache, room_id, thread_id, replacement)
        after = await cache.get_thread_revision(room_id, thread_id)
        assert before is not None
        assert after is not None
        changed_rows = await cache.get_thread_events_written_between(
            room_id,
            thread_id,
            after_write_seq=before.max_write_seq,
            through_write_seq=after.max_write_seq,
            after_thread_write_seq=before.max_thread_write_seq,
            through_thread_write_seq=after.max_thread_write_seq,
        )
    finally:
        await cache.close()

    assert before.event_count == after.event_count == 3
    assert after.max_write_seq == before.max_write_seq
    assert after.max_thread_write_seq > before.max_thread_write_seq
    assert [row["event_id"] for row in changed_rows] == [thread_id, "$new", "$shared"]


@pytest.mark.asyncio
async def test_fetch_thread_history_reuses_durable_mxc_text_after_restart(
    event_cache_factory: Callable[[], ConversationEventCache],
) -> None:
    """Cached full-history reads should reuse durable sidecar text after a restart."""
    cache = event_cache_factory()
    await cache.initialize()

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

        first_history = await fetch_thread_history(
            first_client,
            "!room:localhost",
            "$thread_root",
            event_cache=cache,
        )
    finally:
        await cache.close()

    reopened_cache = event_cache_factory()
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

    assert [message.body for message in first_history] == ["Root message", "Full reply"]
    assert [message.body for message in second_history] == ["Root message", "Full reply"]
    first_client.download.assert_awaited_once_with(mxc="mxc://server/sidecar")
    second_client.download.assert_not_awaited()
