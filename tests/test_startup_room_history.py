"""Tests for the shared per-room startup thread-history operation.

These cover the coalescing contract directly on ``StartupRoomHistoryCoordinator`` and through the
conversation-cache facade and stale-stream auto-resume startup-certification path.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache
from mindroom.matrix.stale_stream_cleanup import (
    _auto_resume_interrupted_threads as auto_resume_interrupted_threads,
)
from mindroom.matrix.stale_stream_cleanup import (
    _InterruptedThread as InterruptedThread,
)
from mindroom.matrix.startup_room_history import (
    StartupRoomHistoryCoordinator,
    StartupRoomScanResult,
    StartupRootOutcome,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection
    from pathlib import Path

ROOM_ID = "!room:localhost"
OTHER_ROOM_ID = "!other:localhost"
PRINCIPAL_ID = "@mindroom_router:localhost"
OTHER_PRINCIPAL_ID = "@mindroom_code:localhost"
USER_ID = "@user:localhost"
BOT_USER_ID = "@actual_test_agent:localhost"


def _stored(thread_root_ids: Collection[str]) -> StartupRoomScanResult:
    """Return a scan result that stored every requested root."""
    return StartupRoomScanResult(outcomes=dict.fromkeys(thread_root_ids, StartupRootOutcome.STORED))


class _RecordingScanner:
    """Scanner that records every scan and can be released on demand."""

    def __init__(self, *, blocking: bool = False) -> None:
        self.scanned_root_sets: list[frozenset[str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._blocking = blocking
        if not blocking:
            self.release.set()

    @property
    def scan_count(self) -> int:
        """Return how many room scans actually ran."""
        return len(self.scanned_root_sets)

    async def __call__(self, _room_id: str, thread_root_ids: frozenset[str]) -> StartupRoomScanResult:
        """Record one scan and report every requested root as stored."""
        self.scanned_root_sets.append(thread_root_ids)
        self.started.set()
        await self.release.wait()
        return _stored(thread_root_ids)


# --------------------------------------------------------------------------------------------
# Coordinator contract
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshness_callers_join_a_running_prewarm_scan() -> None:
    """Callers arriving while prewarm scans must join it instead of fetching again."""
    coordinator = StartupRoomHistoryCoordinator()
    scanner = _RecordingScanner(blocking=True)
    prewarm_roots = [f"$thread-{index}:localhost" for index in range(4)]

    prewarm = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=prewarm_roots,
            scan=scanner,
        ),
    )
    await asyncio.wait_for(scanner.started.wait(), timeout=1.0)
    freshness = [
        asyncio.create_task(
            coordinator.acquire(
                principal_id=PRINCIPAL_ID,
                room_id=ROOM_ID,
                thread_root_ids=[root_id],
                scan=scanner,
            ),
        )
        for root_id in prewarm_roots
    ]
    await asyncio.sleep(0)
    scanner.release.set()
    prewarm_result = await prewarm
    freshness_results = await asyncio.gather(*freshness)

    assert scanner.scan_count == 1
    assert all(prewarm_result[root_id].certified for root_id in prewarm_roots)
    for root_id, result in zip(prewarm_roots, freshness_results, strict=True):
        assert result == {root_id: StartupRootOutcome.STORED}


@pytest.mark.asyncio
async def test_prewarm_observes_completed_freshness_work_without_rescanning() -> None:
    """Prewarm arriving after freshness finished must reuse its outcomes, not rescan."""
    coordinator = StartupRoomHistoryCoordinator()
    scanner = _RecordingScanner()
    root_ids = ["$thread-a:localhost", "$thread-b:localhost"]

    await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=root_ids,
        scan=scanner,
    )
    prewarm_result = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=root_ids,
        scan=scanner,
    )

    assert scanner.scan_count == 1
    assert all(prewarm_result[root_id].certified for root_id in root_ids)


@pytest.mark.asyncio
async def test_roots_contributed_before_scope_freeze_join_the_same_scan() -> None:
    """A caller arriving before the scan starts must widen its scope instead of queueing."""
    coordinator = StartupRoomHistoryCoordinator(room_concurrency=1)
    blocker = _RecordingScanner(blocking=True)
    scanner = _RecordingScanner()

    # Occupy the only room slot so the second room's flight exists but has not frozen its scope.
    blocking = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=OTHER_ROOM_ID,
            thread_root_ids=["$blocker:localhost"],
            scan=blocker,
        ),
    )
    await asyncio.wait_for(blocker.started.wait(), timeout=1.0)
    first = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=["$thread-a:localhost"],
            scan=scanner,
        ),
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=["$thread-b:localhost"],
            scan=scanner,
        ),
    )
    await asyncio.sleep(0)
    blocker.release.set()
    await blocking
    first_result = await first
    second_result = await second

    assert scanner.scan_count == 1
    assert scanner.scanned_root_sets[0] == frozenset({"$thread-a:localhost", "$thread-b:localhost"})
    assert first_result["$thread-a:localhost"].certified
    assert second_result["$thread-b:localhost"].certified


@pytest.mark.asyncio
async def test_failed_room_releases_ownership_and_other_rooms_continue() -> None:
    """A failed scan must stay retryable while unrelated rooms keep making progress."""
    coordinator = StartupRoomHistoryCoordinator()
    attempts = 0

    async def flaky_scan(room_id: str, thread_root_ids: frozenset[str]) -> StartupRoomScanResult:
        nonlocal attempts
        if room_id == ROOM_ID:
            attempts += 1
            if attempts == 1:
                msg = "homeserver unavailable"
                raise RuntimeError(msg)
        return _stored(thread_root_ids)

    failed = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=flaky_scan,
    )
    other_room = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=OTHER_ROOM_ID,
        thread_root_ids=["$other:localhost"],
        scan=flaky_scan,
    )
    retried = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=flaky_scan,
    )

    assert failed == {"$thread:localhost": StartupRootOutcome.FAILED}
    assert other_room["$other:localhost"].certified
    assert retried == {"$thread:localhost": StartupRootOutcome.STORED}
    assert attempts == 2


@pytest.mark.asyncio
async def test_cancelling_one_waiter_keeps_shared_room_work_running() -> None:
    """Cancelling a waiter must not cancel the shared flight other callers depend on."""
    coordinator = StartupRoomHistoryCoordinator()
    scanner = _RecordingScanner(blocking=True)

    cancelled_caller = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=["$thread:localhost"],
            scan=scanner,
        ),
    )
    await asyncio.wait_for(scanner.started.wait(), timeout=1.0)
    surviving_caller = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=["$thread:localhost"],
            scan=scanner,
        ),
    )
    await asyncio.sleep(0)
    cancelled_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller
    scanner.release.set()
    surviving_result = await surviving_caller

    assert scanner.scan_count == 1
    assert surviving_result["$thread:localhost"].certified


@pytest.mark.asyncio
async def test_close_cancels_owned_work_and_leaves_no_claim_behind() -> None:
    """Shutdown must end shared room work cleanly and leave nothing claimed."""
    coordinator = StartupRoomHistoryCoordinator()
    scanner = _RecordingScanner(blocking=True)
    owner = object()

    caller = asyncio.create_task(
        coordinator.acquire(
            principal_id=PRINCIPAL_ID,
            room_id=ROOM_ID,
            thread_root_ids=["$thread:localhost"],
            scan=scanner,
            task_owner=owner,
        ),
    )
    await asyncio.wait_for(scanner.started.wait(), timeout=1.0)
    await coordinator.aclose()
    result = await asyncio.wait_for(caller, timeout=1.0)

    assert result == {"$thread:localhost": StartupRootOutcome.FAILED}
    assert coordinator._states == {}
    assert not coordinator._room_slots.locked()
    await wait_for_background_tasks(timeout=1.0, owner=owner)

    # Ownership was released, so a later attempt may scan the same room again.
    scanner.release.set()
    retried = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=scanner,
    )
    assert retried["$thread:localhost"].certified


@pytest.mark.asyncio
async def test_principals_never_share_one_rooms_startup_work() -> None:
    """Two principals in one room must each certify their own isolated cache namespace."""
    coordinator = StartupRoomHistoryCoordinator()
    scanned_principals: list[str] = []

    def scanner_for(principal_id: str) -> object:
        async def scan(_room_id: str, thread_root_ids: frozenset[str]) -> StartupRoomScanResult:
            scanned_principals.append(principal_id)
            return _stored(thread_root_ids)

        return scan

    first = await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=scanner_for(PRINCIPAL_ID),
    )
    second = await coordinator.acquire(
        principal_id=OTHER_PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=scanner_for(OTHER_PRINCIPAL_ID),
    )

    assert scanned_principals == [PRINCIPAL_ID, OTHER_PRINCIPAL_ID]
    assert first["$thread:localhost"].certified
    assert second["$thread:localhost"].certified


@pytest.mark.asyncio
async def test_advancing_the_generation_recertifies_the_same_room() -> None:
    """A new startup wave must re-scan rooms instead of reusing the previous wave's outcomes."""
    coordinator = StartupRoomHistoryCoordinator()
    scanner = _RecordingScanner()

    await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=scanner,
    )
    coordinator.advance_generation()
    await coordinator.acquire(
        principal_id=PRINCIPAL_ID,
        room_id=ROOM_ID,
        thread_root_ids=["$thread:localhost"],
        scan=scanner,
    )

    assert scanner.scan_count == 2
    assert coordinator.generation == 1


# --------------------------------------------------------------------------------------------
# Conversation-cache scanner
# --------------------------------------------------------------------------------------------


def _conversation_cache(
    tmp_path: Path,
    event_cache: SqliteEventCache,
    *,
    client: object,
    coordinator: StartupRoomHistoryCoordinator | None = None,
) -> MatrixConversationCache:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=[ROOM_ID])},
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
        event_cache_write_coordinator=EventCacheWriteCoordinator(logger=MagicMock()),
        startup_room_history=StartupRoomHistoryCoordinator() if coordinator is None else coordinator,
    )
    return MatrixConversationCache(logger=MagicMock(), runtime=runtime)


@asynccontextmanager
async def _conversation_cache_scope(
    tmp_path: Path,
    *,
    client: object,
) -> AsyncIterator[tuple[MatrixConversationCache, SqliteEventCache]]:
    """Yield one real conversation cache and drain its advisory writes before closing storage."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache, client=client)
    try:
        yield conversation_cache, event_cache
    finally:
        await conversation_cache.runtime.event_cache_write_coordinator.close()
        await conversation_cache.runtime.startup_room_history.aclose()
        await event_cache.close()


def _message_event(
    event_id: str,
    body: str,
    *,
    timestamp: int,
    sender: str = USER_ID,
    thread_root_id: str | None = None,
) -> nio.RoomMessageText:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp,
            "room_id": ROOM_ID,
            "type": "m.room.message",
            "content": content,
        },
    )


def _edit_event(
    event_id: str,
    original_event_id: str,
    *,
    timestamp: int,
    thread_root_id: str,
    body: str,
) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": USER_ID,
            "origin_server_ts": timestamp,
            "room_id": ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": f"* {body}",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
                "m.new_content": {
                    "body": body,
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
        },
    )


def _messages_response(chunk: list[nio.Event], *, end: str | None) -> nio.RoomMessagesResponse:
    return nio.RoomMessagesResponse(room_id=ROOM_ID, chunk=chunk, start="", end=end)


def _paged_room_messages(pages: list[list[nio.Event]]) -> AsyncMock:
    """Return a ``room_messages`` mock serving the given pages, restarting on every new walk.

    Restarting keeps the request count meaningful: an implementation that walks the room once per
    thread simply makes many more requests instead of exhausting a fixed response list.
    """
    responses = [
        _messages_response(page, end=None if index == len(pages) - 1 else f"token-{index}")
        for index, page in enumerate(pages)
    ]
    calls = 0

    async def next_page(*_args: object, **kwargs: object) -> nio.RoomMessagesResponse:
        nonlocal calls
        index = 0 if kwargs.get("start") is None else calls % len(responses)
        calls += 1
        return responses[index]

    return AsyncMock(side_effect=next_page)


@pytest.mark.asyncio
async def test_trusted_cache_needs_no_homeserver_request(tmp_path: Path) -> None:
    """Roots whose durable snapshot is already trusted must not trigger a room scan."""
    client = MagicMock()
    client.room_messages = AsyncMock(side_effect=AssertionError("trusted roots must not paginate"))
    root_id = "$thread:localhost"

    async with _conversation_cache_scope(tmp_path, client=client) as (conversation_cache, event_cache):
        stored = await event_cache.replace_thread_if_not_newer(
            ROOM_ID,
            root_id,
            [_message_event(root_id, "root", timestamp=1000).source],
            expected_membership_epoch=await event_cache.room_membership_epoch(ROOM_ID) or 0,
            fetch_started_at=0.0,
        )
        assert stored

        outcomes = await conversation_cache.ensure_startup_thread_history(ROOM_ID, [root_id])

    assert outcomes == {root_id: StartupRootOutcome.ALREADY_TRUSTED}
    client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_truncated_scan_certifies_found_roots_only(tmp_path: Path) -> None:
    """A page-bounded scan may warm what it found and must not certify what it never reached."""
    found_root_id = "$found:localhost"
    unreached_root_id = "$unreached:localhost"
    client = MagicMock()
    client.room_messages = _paged_room_messages(
        [
            [_message_event(found_root_id, "root", timestamp=2000)],
            [_message_event("$filler:localhost", "x", timestamp=1)],
        ],
    )

    async with _conversation_cache_scope(tmp_path, client=client) as (conversation_cache, _event_cache):
        with patch("mindroom.matrix.conversation_cache._STARTUP_PREWARM_MAX_SCAN_PAGES", 1):
            outcomes = await conversation_cache.ensure_startup_thread_history(
                ROOM_ID,
                [found_root_id, unreached_root_id],
            )

    assert outcomes[found_root_id] is StartupRootOutcome.STORED
    assert outcomes[unreached_root_id] is StartupRootOutcome.TRUNCATED
    assert not outcomes[unreached_root_id].certified
    assert client.room_messages.await_count == 1


@pytest.mark.asyncio
async def test_edited_thread_snapshot_matches_canonical_visible_body(tmp_path: Path) -> None:
    """Snapshots warmed by the shared scan must carry the same visible bodies as a strict read."""
    root_id = "$root:localhost"
    reply_id = "$reply:localhost"
    client = MagicMock()
    client.room_messages = _paged_room_messages(
        [
            [
                _edit_event(
                    "$reply-edit:localhost",
                    reply_id,
                    timestamp=4000,
                    thread_root_id=root_id,
                    body="edited reply",
                ),
                _message_event(reply_id, "original reply", timestamp=3000, thread_root_id=root_id),
                _message_event(root_id, "root", timestamp=1000),
            ],
        ],
    )

    async with _conversation_cache_scope(tmp_path, client=client) as (conversation_cache, _event_cache):
        outcomes = await conversation_cache.ensure_startup_thread_history(ROOM_ID, [root_id])
        assert outcomes == {root_id: StartupRootOutcome.STORED}

        history = await conversation_cache.get_strict_thread_history(
            ROOM_ID,
            root_id,
            caller_label="test_startup_room_history",
        )

    assert [message.body for message in history] == ["root", "edited reply"]
    # The cached snapshot served the strict read, so no extra pagination happened.
    assert client.room_messages.await_count == 1


# --------------------------------------------------------------------------------------------
# Auto-resume end to end
# --------------------------------------------------------------------------------------------


def _auto_resume_config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": {"display_name": "Test Agent", "rooms": [ROOM_ID]}},
            authorization={"default_room_access": True, "agent_reply_permissions": {}},
            mindroom_user={"username": "mindroom", "display_name": "MindRoom"},
        ),
        runtime_paths,
    )
    config.defaults.auto_resume_after_restart = True
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "actual_router", "test_agent": "actual_test_agent"},
    )
    return config


def _interrupted(root_id: str, target_event_id: str, *, timestamp_ms: int) -> InterruptedThread:
    return InterruptedThread(
        room_id=ROOM_ID,
        thread_id=root_id,
        target_event_id=target_event_id,
        partial_text="partial",
        agent_name="test_agent",
        original_sender_id=USER_ID,
        timestamp_ms=timestamp_ms,
    )


@pytest.mark.asyncio
async def test_fifty_interrupted_threads_share_one_startup_certification_scan(tmp_path: Path) -> None:
    """Many interrupted threads must share one startup-certification room scan."""
    config = _auto_resume_config(tmp_path)
    thread_count = 50
    root_ids = [f"$root-{index:02d}:localhost" for index in range(thread_count)]
    target_ids = [f"$target-{index:02d}:localhost" for index in range(thread_count)]
    page: list[nio.Event] = []
    for index, (root_id, target_id) in enumerate(zip(root_ids, target_ids, strict=True)):
        page.append(
            _message_event(
                target_id,
                "interrupted",
                timestamp=2000 + index,
                sender=BOT_USER_ID,
                thread_root_id=root_id,
            ),
        )
        page.append(_message_event(root_id, "please help", timestamp=1000 + index))
    client = MagicMock()
    client.room_messages = _paged_room_messages([page])
    interrupted = [
        _interrupted(root_id, target_id, timestamp_ms=2000 + index)
        for index, (root_id, target_id) in enumerate(zip(root_ids, target_ids, strict=True))
    ]

    async with _conversation_cache_scope(tmp_path, client=client) as (conversation_cache, _event_cache):
        with (
            patch(
                "mindroom.matrix.stale_stream_cleanup._interrupted_target_remains_latest_human_work",
                new=AsyncMock(return_value=True),
            ) as refresh_freshness,
            patch(
                "mindroom.matrix.stale_stream_cleanup.send_message_result",
                new=AsyncMock(side_effect=delivered_matrix_side_effect("$auto-resume")),
            ) as send_resume,
        ):
            resumed_count = await auto_resume_interrupted_threads(
                client,
                interrupted,
                config=config,
                runtime_paths=runtime_paths_for(config),
                conversation_cache=conversation_cache,
                delay=0,
            )

    assert resumed_count == thread_count
    assert send_resume.await_count == thread_count
    assert refresh_freshness.await_count == thread_count
    assert client.room_messages.await_count == 1


@pytest.mark.asyncio
async def test_uncertified_thread_history_skips_auto_resume(tmp_path: Path) -> None:
    """A root the shared scan could not certify must never justify a resume."""
    config = _auto_resume_config(tmp_path)
    conversation_cache = AsyncMock()
    conversation_cache.ensure_startup_thread_history = AsyncMock(
        return_value={"$root:localhost": StartupRootOutcome.TRUNCATED},
    )
    conversation_cache.refresh_strict_thread_history_from_source = AsyncMock(
        side_effect=AssertionError("an uncertified root must not start its own source refresh"),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$auto-resume")),
    ) as send_resume:
        resumed_count = await auto_resume_interrupted_threads(
            MagicMock(),
            [_interrupted("$root:localhost", "$target:localhost", timestamp_ms=2000)],
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
            delay=0,
        )

    assert resumed_count == 0
    conversation_cache.refresh_strict_thread_history_from_source.assert_not_awaited()
    send_resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_certification_failure_falls_back_to_the_per_thread_freshness_read(tmp_path: Path) -> None:
    """A broken shared operation must not silently block every resume."""
    config = _auto_resume_config(tmp_path)
    conversation_cache = AsyncMock()
    conversation_cache.ensure_startup_thread_history = AsyncMock(side_effect=RuntimeError("coordinator down"))
    conversation_cache.refresh_strict_thread_history_from_source = AsyncMock(
        side_effect=RuntimeError("history unavailable"),
    )

    with patch(
        "mindroom.matrix.stale_stream_cleanup.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$auto-resume")),
    ) as send_resume:
        resumed_count = await auto_resume_interrupted_threads(
            MagicMock(),
            [_interrupted("$root:localhost", "$target:localhost", timestamp_ms=2000)],
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_cache=conversation_cache,
            delay=0,
        )

    # Fail open to the per-thread read, which itself stays fail-closed on an unusable history.
    conversation_cache.refresh_strict_thread_history_from_source.assert_awaited_once()
    assert resumed_count == 0
    send_resume.assert_not_awaited()
