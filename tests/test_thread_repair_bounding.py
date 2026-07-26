"""Bounding, deduplication, and priority tests for speculative thread-cache repair.

Speculative repair is launched by a live append that finds no cached snapshot. It is singleflighted
per thread, but a burst of appends across many threads previously launched an unbounded number of
full history scans, and the same thread was rescanned once per append because the flight was only
deduplicated while it was still running.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.cache import ThreadCacheReplaceOutcome
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_repair import (
    MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS,
    MAX_CONCURRENT_THREAD_REPAIRS,
    ThreadRepairRegistry,
    ThreadRepairSuppressedError,
)
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache, is_sync_replay_batch
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.matrix.cache import ConversationEventCache

ROOM_ID = "!room:localhost"


def _conversation_cache(tmp_path: Path, event_cache: ConversationEventCache) -> MatrixConversationCache:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=[ROOM_ID])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    runtime = BotRuntimeState(
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=event_cache,
        event_cache_write_coordinator=None,
    )
    return MatrixConversationCache(logger=MagicMock(), runtime=runtime)


def _event_source(event_id: str, *, thread_id: str) -> dict[str, Any]:
    content: dict[str, Any] = {"body": event_id, "msgtype": "m.text"}
    if event_id != thread_id:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": content,
    }


def _fetch_result(thread_id: str) -> MagicMock:
    return MagicMock(
        history=[],
        event_sources=[_event_source(thread_id, thread_id=thread_id)],
        fetch_ms=1.0,
        room_scan_pages=1,
        scanned_event_count=1,
        resolution_ms=1.0,
        sidecar_hydration_ms=0.0,
    )


class _ScanRecorder:
    """Count homeserver history scans and observe how many run at the same time."""

    def __init__(self, *, scan_seconds: float = 0.01) -> None:
        self.scanned_thread_ids: list[str] = []
        self.peak_concurrent_scans = 0
        self._scan_seconds = scan_seconds
        self._in_flight = 0

    async def scan(
        self,
        _client: object,
        _room_id: str,
        thread_id: str,
        **_kwargs: object,
    ) -> MagicMock:
        self._in_flight += 1
        self.peak_concurrent_scans = max(self.peak_concurrent_scans, self._in_flight)
        self.scanned_thread_ids.append(thread_id)
        try:
            await asyncio.sleep(self._scan_seconds)
            return _fetch_result(thread_id)
        finally:
            self._in_flight -= 1

    @property
    def rescan_counts(self) -> Counter[str]:
        return Counter(self.scanned_thread_ids)


@pytest.mark.asyncio
async def test_missing_cache_append_storm_scans_each_thread_at_most_once(tmp_path: Path) -> None:
    """A replay-sized burst of missing-cache appends must not scan one thread once per event."""
    thread_count = 6
    appends_per_thread = 10
    thread_ids = [f"$thread-{index}:localhost" for index in range(thread_count)]

    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    recorder = _ScanRecorder()

    # A snapshot that loses the guarded replacement race is exactly what a concurrent live append
    # produces, and it leaves the thread without a usable cache for the next append to find.
    invalidated_store = AsyncMock(return_value=ThreadCacheReplaceOutcome.INVALIDATED)

    try:
        with (
            patch.object(event_cache, "replace_thread_if_not_newer", invalidated_store),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                AsyncMock(side_effect=recorder.scan),
            ),
        ):
            # Replay delivers appends over time, so each wave lands after the previous repair
            # finished. Singleflight only collapses callers that overlap one running flight.
            for _wave in range(appends_per_thread):
                for thread_id in thread_ids:
                    conversation_cache._schedule_missing_thread_repair(ROOM_ID, thread_id)
                await wait_for_background_tasks(timeout=5.0, owner=coordinator.background_task_owner)
            await coordinator.close()
    finally:
        await event_cache.close()

    assert recorder.rescan_counts.most_common(1)[0][1] == 1, (
        f"each thread must be scanned at most once per burst, saw {recorder.rescan_counts.most_common(3)}"
    )
    assert len(recorder.scanned_thread_ids) <= thread_count


@pytest.mark.asyncio
async def test_missing_cache_append_storm_bounds_concurrent_history_scans(tmp_path: Path) -> None:
    """Speculative repair fan-out must stay under a global cap however many threads are stale."""
    thread_count = 40
    thread_ids = [f"$thread-{index}:localhost" for index in range(thread_count)]

    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    recorder = _ScanRecorder(scan_seconds=0.05)
    invalidated_store = AsyncMock(return_value=ThreadCacheReplaceOutcome.INVALIDATED)

    try:
        with (
            patch.object(event_cache, "replace_thread_if_not_newer", invalidated_store),
            patch(
                "mindroom.matrix.client_thread_history._fetch_thread_history_with_events",
                AsyncMock(side_effect=recorder.scan),
            ),
        ):
            for thread_id in thread_ids:
                conversation_cache._schedule_missing_thread_repair(ROOM_ID, thread_id)
            await coordinator.close()
    finally:
        await event_cache.close()

    assert recorder.peak_concurrent_scans <= MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS, (
        f"peak concurrent history scans {recorder.peak_concurrent_scans} exceeded the speculative repair "
        f"budget of {MAX_CONCURRENT_SPECULATIVE_THREAD_REPAIRS} across {thread_count} stale threads"
    )
    assert recorder.peak_concurrent_scans <= MAX_CONCURRENT_THREAD_REPAIRS
    assert len(recorder.scanned_thread_ids) >= 1


def _schedule[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
    return asyncio.create_task(repair_factory())


def _flight_key(thread_id: str, *, hydrate_sidecars: bool = True, allow_stale_fallback: bool = False) -> tuple:
    return ("@agent:localhost", ROOM_ID, thread_id, hydrate_sidecars, allow_stale_fallback)


@pytest.mark.asyncio
async def test_speculative_repair_does_not_open_a_second_scan_for_another_caller_contract() -> None:
    """The flight key names a caller contract, so it must not be what admits a speculative scan."""
    registry = ThreadRepairRegistry()
    scan_count = 0
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def repair() -> str:
        nonlocal scan_count
        scan_count += 1
        scan_started.set()
        await release_scan.wait()
        return "scanned"

    read_flight = asyncio.create_task(
        registry.run(
            _flight_key("$thread", hydrate_sidecars=True),
            schedule=_schedule,
            repair=repair,
            result_arms_backoff=lambda _result: False,
        ),
    )
    try:
        await asyncio.wait_for(scan_started.wait(), timeout=1.0)
        with pytest.raises(ThreadRepairSuppressedError) as suppressed:
            # A live append repairs the same thread under the snapshot contract, not the read one.
            await registry.run(
                _flight_key("$thread", hydrate_sidecars=False),
                schedule=_schedule,
                repair=repair,
                result_arms_backoff=lambda _result: False,
                speculative=True,
            )
    finally:
        release_scan.set()
        await read_flight

    assert suppressed.value.reason == "repair_in_flight"
    assert scan_count == 1


@pytest.mark.asyncio
async def test_speculative_repair_waits_out_a_cooldown_instead_of_rescanning_each_append() -> None:
    """A completed scan must suppress the next append's repair, and only for a bounded window."""
    now = 100.0
    registry = ThreadRepairRegistry(speculative_cooldown_seconds=30.0, clock=lambda: now)
    repair = AsyncMock(return_value="scanned")
    key = _flight_key("$thread", hydrate_sidecars=False)

    await registry.run(key, schedule=_schedule, repair=repair, result_arms_backoff=lambda _r: False, speculative=True)
    with pytest.raises(ThreadRepairSuppressedError) as suppressed:
        await registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_arms_backoff=lambda _r: False,
            speculative=True,
        )

    assert suppressed.value.reason == "recently_repaired"
    assert repair.await_count == 1

    now = 131.0
    await registry.run(key, schedule=_schedule, repair=repair, result_arms_backoff=lambda _r: False, speculative=True)

    assert repair.await_count == 2


@pytest.mark.asyncio
async def test_cooldown_never_blocks_the_read_that_a_dispatch_is_waiting_on() -> None:
    """Bounding speculative work must not stop a genuinely missing thread from being repaired."""
    now = 100.0
    registry = ThreadRepairRegistry(speculative_cooldown_seconds=30.0, clock=lambda: now)
    repair = AsyncMock(return_value="scanned")
    key = _flight_key("$thread", hydrate_sidecars=False)

    await registry.run(key, schedule=_schedule, repair=repair, result_arms_backoff=lambda _r: False, speculative=True)
    await registry.run(key, schedule=_schedule, repair=repair, result_arms_backoff=lambda _r: False)

    assert repair.await_count == 2


@pytest.mark.asyncio
async def test_interactive_repair_is_never_starved_by_speculative_repairs() -> None:
    """Speculative repairs must yield the global cap to a caller a dispatch is blocked on."""
    registry = ThreadRepairRegistry(max_concurrent_repairs=2, max_concurrent_speculative_repairs=1)
    release = asyncio.Event()
    cap_reached = asyncio.Event()
    started: list[str] = []

    def slow_repair(label: str) -> Callable[[], Awaitable[str]]:
        async def repair() -> str:
            started.append(label)
            if len(started) == 2:
                cap_reached.set()
            await release.wait()
            return label

        return repair

    speculative = asyncio.create_task(
        registry.run(
            _flight_key("$speculative"),
            schedule=_schedule,
            repair=slow_repair("speculative"),
            result_arms_backoff=lambda _r: False,
            speculative=True,
        ),
    )
    interactive = asyncio.create_task(
        registry.run(
            _flight_key("$interactive"),
            schedule=_schedule,
            repair=slow_repair("interactive"),
            result_arms_backoff=lambda _r: False,
        ),
    )
    queued_interactive = asyncio.create_task(
        registry.run(
            _flight_key("$queued"),
            schedule=_schedule,
            repair=slow_repair("queued_interactive"),
            result_arms_backoff=lambda _r: False,
        ),
    )
    try:
        await asyncio.wait_for(cap_reached.wait(), timeout=1.0)
        # The global cap is now full and one interactive caller is queued behind it.
        with pytest.raises(ThreadRepairSuppressedError) as suppressed:
            await registry.run(
                _flight_key("$another"),
                schedule=_schedule,
                repair=slow_repair("declined"),
                result_arms_backoff=lambda _r: False,
                speculative=True,
            )
    finally:
        release.set()
        await asyncio.gather(speculative, interactive, queued_interactive)

    assert suppressed.value.reason in {"speculative_concurrency_limit", "repair_concurrency_limit"}
    assert started == ["speculative", "interactive", "queued_interactive"]
    assert "declined" not in started


@pytest.mark.asyncio
async def test_sync_replay_suppresses_speculative_repair_but_not_interactive_repair() -> None:
    """Replay must not launch speculative scans, and must not block a waiting reader either."""
    registry = ThreadRepairRegistry()
    repair = AsyncMock(return_value="scanned")

    with registry.suppress_speculative_repairs():
        with pytest.raises(ThreadRepairSuppressedError) as suppressed:
            await registry.run(
                _flight_key("$thread"),
                schedule=_schedule,
                repair=repair,
                result_arms_backoff=lambda _r: False,
                speculative=True,
            )
        interactive = await registry.run(
            _flight_key("$other"),
            schedule=_schedule,
            repair=repair,
            result_arms_backoff=lambda _r: False,
        )

    assert suppressed.value.reason == "sync_replay"
    assert interactive == "scanned"
    assert repair.await_count == 1

    await registry.run(
        _flight_key("$thread"),
        schedule=_schedule,
        repair=repair,
        result_arms_backoff=lambda _r: False,
        speculative=True,
    )

    assert repair.await_count == 2


def _sync_response(*, event_count: int, limited: bool) -> MagicMock:
    timeline = MagicMock(limited=limited, events=[MagicMock() for _ in range(event_count)])
    response = MagicMock()
    response.rooms.join = {ROOM_ID: MagicMock(timeline=timeline)}
    return response


@pytest.mark.parametrize(
    ("event_count", "limited", "expected"),
    [
        (3, False, False),
        (3, True, True),
        (200, False, True),
    ],
)
def test_sync_replay_batch_detection(event_count: int, limited: bool, expected: bool) -> None:
    """Only gap catch-up batches should switch speculative repair off."""
    assert is_sync_replay_batch(_sync_response(event_count=event_count, limited=limited)) is expected


@pytest.mark.asyncio
async def test_sync_certification_declines_speculative_repair_for_a_replay_batch(tmp_path: Path) -> None:
    """The sync facade must hold repairs off for exactly the span of one replay batch."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    suppression_reasons: list[str | None] = []

    async def observe_suppression(_response: object) -> str:
        suppression_reasons.append(
            coordinator.speculative_thread_repair_suppression_reason(
                ROOM_ID,
                "$thread:localhost",
                coordination_scope=event_cache.principal_id,
            ),
        )
        return "certified"

    try:
        with patch.object(
            conversation_cache._sync,
            "cache_sync_timeline_for_certification",
            AsyncMock(side_effect=observe_suppression),
        ):
            await conversation_cache.cache_sync_timeline_for_certification(
                _sync_response(event_count=3, limited=True),
            )
            await conversation_cache.cache_sync_timeline_for_certification(
                _sync_response(event_count=3, limited=False),
            )
    finally:
        await coordinator.close()
        await event_cache.close()

    assert suppression_reasons == ["sync_replay", None]
