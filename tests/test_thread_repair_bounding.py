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

import nio
import pytest

from mindroom.background_tasks import _tasks_for_owner, wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.cache import ThreadCacheReplaceOutcome
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_repair import (
    ThreadRepairRegistry,
    ThreadRepairSuppressedError,
)
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator
from mindroom.matrix.conversation_cache import MatrixConversationCache, _is_sync_replay_batch
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

    budgets = ThreadRepairRegistry()
    assert recorder.peak_concurrent_scans <= budgets.max_concurrent_speculative_repairs, (
        f"peak concurrent history scans {recorder.peak_concurrent_scans} exceeded the speculative repair "
        f"budget of {budgets.max_concurrent_speculative_repairs} across {thread_count} stale threads"
    )
    assert recorder.peak_concurrent_scans <= budgets.max_concurrent_repairs
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


def _sync_response(*, event_count: int, limited: bool) -> nio.SyncResponse:
    timeline = nio.responses.Timeline(
        events=[_room_message(f"$event-{index}:localhost") for index in range(event_count)],
        limited=limited,
        prev_batch="s0",
    )
    room_info = nio.responses.RoomInfo(timeline=timeline, state=[], ephemeral=[], account_data=[])
    return nio.SyncResponse(
        next_batch="s1",
        rooms=nio.responses.Rooms(invite={}, join={ROOM_ID: room_info}, leave={}),
        device_key_count=nio.responses.DeviceOneTimeKeyCount(curve25519=0, signed_curve25519=0),
        device_list=nio.responses.DeviceList(changed=[], left=[]),
        to_device_events=[],
        presence_events=[],
    )


def _room_message(event_id: str) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"body": "hello", "msgtype": "m.text"},
        },
    )


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
    assert _is_sync_replay_batch(_sync_response(event_count=event_count, limited=limited)) is expected


@pytest.mark.asyncio
async def test_sync_certification_declines_speculative_repair_for_a_replay_batch(tmp_path: Path) -> None:
    """The sync facade must hold repairs off for exactly the span of one replay batch."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    could_schedule: list[bool] = []

    async def observe_suppression(_response: object) -> str:
        reserved = coordinator.reserve_speculative_thread_repair(
            ROOM_ID,
            "$thread:localhost",
            coordination_scope=event_cache.principal_id,
        )
        could_schedule.append(reserved is not None)
        if reserved is not None:
            coordinator.release_speculative_thread_repair(
                ROOM_ID,
                "$thread:localhost",
                coordination_scope=event_cache.principal_id,
                token=reserved,
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

    assert could_schedule == [False, True]


@pytest.mark.asyncio
async def test_interactive_join_keeps_a_declined_speculative_flight_running() -> None:
    """A read joined to a speculative flight must not inherit its suppression."""
    registry = ThreadRepairRegistry()
    key = _flight_key("$thread", hydrate_sidecars=False)
    gate = asyncio.Event()
    scans = 0

    async def scan() -> str:
        nonlocal scans
        scans += 1
        return "history"

    def delayed_schedule[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def runner() -> T:
            await gate.wait()
            return await repair_factory()

        return asyncio.create_task(runner())

    speculative = asyncio.create_task(
        registry.run(
            key,
            schedule=delayed_schedule,
            repair=scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
        ),
    )
    await asyncio.sleep(0)
    interactive = asyncio.create_task(
        registry.run(key, schedule=_schedule, repair=scan, result_arms_backoff=lambda _r: False),
    )
    await asyncio.sleep(0)

    # A replay batch begins while the admitted flight is still queued behind the write barrier.
    with registry.suppress_speculative_repairs():
        gate.set()
        results = await asyncio.gather(speculative, interactive)

    assert results == ["history", "history"]
    assert scans == 1


@pytest.mark.asyncio
async def test_clearing_the_registry_resets_every_admission_gate() -> None:
    """State left behind by `clear` would silently disable speculative repair for the process."""
    registry = ThreadRepairRegistry(max_concurrent_speculative_repairs=1)
    thread_key = ("@agent:localhost", ROOM_ID, "$thread")
    await registry.run(
        _flight_key("$thread", hydrate_sidecars=False),
        schedule=_schedule,
        repair=AsyncMock(return_value="scanned"),
        result_arms_backoff=lambda _r: False,
        speculative=True,
    )
    # Every gate must be non-empty before the clear, or asserting it is empty afterwards proves
    # nothing. The cooldown is populated by the repair above; the rest are seeded here.
    assert registry.speculative_suppression_reason(thread_key) == "recently_repaired"
    registry._running_speculative_repairs = 1
    registry._interactive_joins[_flight_key("$thread", hydrate_sidecars=False)] = 1

    registry.clear()

    assert registry.speculative_suppression_reason(thread_key) is None
    assert registry._speculative_cooldowns == {}
    assert registry._interactive_joins == {}
    assert registry._running_speculative_repairs == 0


@pytest.mark.asyncio
async def test_clearing_one_room_forgets_its_cooldowns() -> None:
    """A departed room must not keep holding its threads inside a repair cooldown."""
    registry = ThreadRepairRegistry()
    await registry.run(
        _flight_key("$thread", hydrate_sidecars=False),
        schedule=_schedule,
        repair=AsyncMock(return_value="scanned"),
        result_arms_backoff=lambda _r: False,
        speculative=True,
    )
    assert registry.speculative_suppression_reason(("@agent:localhost", ROOM_ID, "$thread")) == "recently_repaired"

    registry.clear_room("@agent:localhost", ROOM_ID)

    assert registry.speculative_suppression_reason(("@agent:localhost", ROOM_ID, "$thread")) is None


@pytest.mark.asyncio
async def test_clearing_the_registry_does_not_inflate_the_repair_ceiling() -> None:
    """A repair outliving `clear` must return its permit to the ceiling it took one from.

    `clear` runs from `close`, whose drain can give up while a repair is still holding a permit.
    Replacing the semaphore there would let that survivor release into the replacement and raise the
    bound above its own maximum, permanently and with nothing to clamp it.
    """
    registry = ThreadRepairRegistry(max_concurrent_repairs=2)
    await registry._acquire_repair_slot(speculative=False)

    registry.clear()
    registry._release_repair_slot(speculative=False)

    assert registry._repair_slots._value == 2


@pytest.mark.asyncio
async def test_a_replay_burst_does_not_create_a_task_per_event(tmp_path: Path) -> None:
    """Scheduling must be bounded before admission, not only at the scan.

    A replay reaches the scheduler once per event with no await in between, so nothing has entered
    the registry yet: a check that does not also claim lets every caller believe it has capacity.
    """
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    budget = ThreadRepairRegistry().max_concurrent_speculative_repairs

    try:
        for index in range(250):
            conversation_cache._schedule_missing_thread_repair(ROOM_ID, f"$thread-{index}:localhost")
        distinct_threads = len(_tasks_for_owner(coordinator.background_task_owner))
        for _ in range(250):
            conversation_cache._schedule_missing_thread_repair(ROOM_ID, "$hot:localhost")
        one_thread = len(_tasks_for_owner(coordinator.background_task_owner))
    finally:
        await event_cache.close()

    assert distinct_threads <= budget, f"250 stale threads created {distinct_threads} tasks before admission"
    assert one_thread == distinct_threads, "repeated events for one thread each added a task"


async def _drain_done_callbacks() -> None:
    """Let task done callbacks run.

    They are queued with ``call_soon``, so awaiting a cancelled task does not by itself guarantee
    its cleanup has executed.
    """
    for _ in range(3):
        await asyncio.sleep(0)


def _pre_start_cancelled_repair(conversation_cache: MatrixConversationCache, thread_id: str) -> asyncio.Task[None]:
    """Schedule a speculative repair and cancel it before its coroutine ever runs.

    Identified by set difference: background tasks are held in a set, so indexing the owner's tasks
    picks an arbitrary one and can leave the repair just scheduled running.
    """
    coordinator = conversation_cache.runtime.event_cache_write_coordinator
    assert coordinator is not None
    before = set(_tasks_for_owner(coordinator.background_task_owner))
    conversation_cache._schedule_missing_thread_repair(ROOM_ID, thread_id)
    created = set(_tasks_for_owner(coordinator.background_task_owner)) - before
    assert len(created) == 1, f"expected exactly one scheduled repair, got {len(created)}"
    scheduled = created.pop()
    scheduled.cancel()
    return scheduled


@pytest.mark.asyncio
async def test_a_repair_cancelled_before_it_starts_releases_its_claim(tmp_path: Path) -> None:
    """A body-level release never runs for a task cancelled before its first instruction."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator

    try:
        cancelled = _pre_start_cancelled_repair(conversation_cache, "$thread:localhost")
        await asyncio.gather(cancelled, return_exceptions=True)
        await _drain_done_callbacks()
        # The claim must be gone, so the very same thread can be scheduled again.
        conversation_cache._schedule_missing_thread_repair(ROOM_ID, "$thread:localhost")
        replacements = [task for task in _tasks_for_owner(coordinator.background_task_owner) if not task.done()]
        for task in _tasks_for_owner(coordinator.background_task_owner):
            task.cancel()
        await asyncio.gather(*_tasks_for_owner(coordinator.background_task_owner), return_exceptions=True)
    finally:
        await event_cache.close()

    assert replacements, "the leaked claim blocked a replacement repair for the same thread"


@pytest.mark.asyncio
async def test_pre_start_cancelled_repairs_do_not_suppress_unrelated_threads(tmp_path: Path) -> None:
    """Leaked claims fill the pending budget and starve threads that never failed."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    budget = ThreadRepairRegistry().max_concurrent_speculative_repairs

    try:
        cancelled = [
            _pre_start_cancelled_repair(conversation_cache, f"$dead-{index}:localhost") for index in range(budget)
        ]
        await asyncio.gather(*cancelled, return_exceptions=True)
        await _drain_done_callbacks()
        leaked = dict(coordinator._thread_repairs._reserved_speculative)
        unrelated = coordinator.reserve_speculative_thread_repair(
            ROOM_ID,
            "$unrelated:localhost",
            coordination_scope=event_cache.principal_id,
        )
        for task in _tasks_for_owner(coordinator.background_task_owner):
            task.cancel()
        await asyncio.gather(*_tasks_for_owner(coordinator.background_task_owner), return_exceptions=True)
    finally:
        await event_cache.close()

    assert leaked == {}, f"cancelled repairs leaked claims: {sorted(key[2] for key in leaked)}"
    assert unrelated is not None, f"{budget} cancelled repairs exhausted the pending budget"


@pytest.mark.asyncio
async def test_waves_during_preparation_create_one_pre_admission_task(tmp_path: Path) -> None:
    """The claim has to outlive the awaited preparation, not just the synchronous burst."""
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()
    conversation_cache = _conversation_cache(tmp_path, event_cache)
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    conversation_cache.runtime.event_cache_write_coordinator = coordinator
    prepare_calls = 0
    block_preparation = asyncio.Event()

    async def blocked_prepare(*_args: object, **_kwargs: object) -> None:
        nonlocal prepare_calls
        prepare_calls += 1
        await block_preparation.wait()

    try:
        with patch.object(conversation_cache, "_prepare_pending_thread_repair_deltas", blocked_prepare):
            for _wave in range(50):
                for _ in range(250):
                    conversation_cache._schedule_missing_thread_repair(ROOM_ID, "$hot:localhost")
                await asyncio.sleep(0)  # let the scheduled task reach preparation and block there
            pending = [task for task in _tasks_for_owner(coordinator.background_task_owner) if not task.done()]
            blocked = prepare_calls
        block_preparation.set()
        for task in _tasks_for_owner(coordinator.background_task_owner):
            task.cancel()
        await asyncio.gather(*_tasks_for_owner(coordinator.background_task_owner), return_exceptions=True)
    finally:
        await event_cache.close()

    assert blocked == 1, f"preparation ran {blocked} times for one thread"
    assert len(pending) == 1, f"{len(pending)} pre-admission tasks for one thread across 50 waves"


@pytest.mark.asyncio
async def test_a_stale_release_cannot_drop_a_later_claim_on_the_same_thread() -> None:
    """Releases are identified by token, so a late one cannot free somebody else's claim."""
    registry = ThreadRepairRegistry()
    key = ("@agent:localhost", ROOM_ID, "$thread")

    first = registry.reserve_speculative_repair(key)
    assert first is not None
    registry.release_speculative_repair(key, first)
    second = registry.reserve_speculative_repair(key)
    assert second is not None

    registry.release_speculative_repair(key, first)  # the abandoned holder, arriving late

    assert registry.speculative_suppression_reason(key) == "repair_pending"
    registry.release_speculative_repair(key, second)
    assert registry.speculative_suppression_reason(key) is None
    registry.clear()
    assert registry._reserved_speculative == {}


@pytest.mark.asyncio
async def test_flights_queued_behind_the_barrier_stay_globally_bounded() -> None:
    """Registering a flight is not admission: the queued body still has to reach the scan gates.

    ``schedule`` only queues the repair behind the coordinator barrier. Until that body starts it
    holds no slot and counts against no budget, so releasing the scheduling claim at registration
    would leave the whole wait unbounded and let every wave add another flight.
    """
    registry = ThreadRepairRegistry()
    budget = registry.max_concurrent_speculative_repairs
    barrier = asyncio.Event()
    bodies_started = 0

    def queued_behind_barrier[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def runner() -> T:
            await barrier.wait()
            nonlocal bodies_started
            bodies_started += 1
            return await repair_factory()

        return asyncio.create_task(runner())

    async def scan() -> str:
        return "scanned"

    flights: list[asyncio.Task[str]] = []
    try:
        for wave in range(20):
            for slot in range(budget):
                thread_id = f"$w{wave}-t{slot}"
                if registry.reserve_speculative_repair(("@agent:localhost", ROOM_ID, thread_id)) is None:
                    continue
                flights.append(
                    asyncio.create_task(
                        registry.run(
                            _flight_key(thread_id, hydrate_sidecars=False),
                            schedule=queued_behind_barrier,
                            repair=scan,
                            result_arms_backoff=lambda _r: False,
                            speculative=True,
                        ),
                    ),
                )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        queued_flights = len(registry._tasks)
        started_before_release = bodies_started
    finally:
        barrier.set()
        await asyncio.gather(*flights, return_exceptions=True)

    assert started_before_release == 0, "the barrier did not hold, so this proves nothing"
    assert queued_flights <= budget, (
        f"{queued_flights} flights queued behind the barrier against a speculative budget of {budget}"
    )


@pytest.mark.asyncio
async def test_cancelling_the_scheduler_after_registration_keeps_the_flight_bounded() -> None:
    """The scheduling task stops owning the claim once it has registered a flight.

    ``run`` shields the flight, so cancelling the scheduling task leaves that flight queued behind
    the coordinator barrier. Releasing the claim from the scheduler's done callback would drop the
    only thing bounding it, and every wave could add another.
    """
    registry = ThreadRepairRegistry()
    budget = registry.max_concurrent_speculative_repairs
    barrier = asyncio.Event()

    def queued_behind_barrier[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def runner() -> T:
            await barrier.wait()
            return await repair_factory()

        return asyncio.create_task(runner())

    async def scan() -> str:
        return "scanned"

    outers: list[asyncio.Task[str]] = []
    try:
        for wave in range(20):
            for slot in range(budget):
                thread_id = f"$w{wave}-t{slot}"
                thread_key = ("@agent:localhost", ROOM_ID, thread_id)
                token = registry.reserve_speculative_repair(thread_key)
                if token is None:
                    continue
                outer = asyncio.create_task(
                    registry.run(
                        _flight_key(thread_id, hydrate_sidecars=False),
                        schedule=queued_behind_barrier,
                        repair=scan,
                        result_arms_backoff=lambda _r: False,
                        speculative=True,
                        claim_token=token,
                    ),
                )
                outers.append(outer)
                outer.add_done_callback(
                    lambda _task, key=thread_key, tok=token: registry.release_speculative_repair(key, tok),
                )
            await asyncio.sleep(0)
            # Cancel the schedulers only once their flights are registered and queued.
            for outer in outers:
                outer.cancel()
            await asyncio.gather(*outers, return_exceptions=True)
            await _drain_done_callbacks()

        queued_flights = len(registry._tasks)
    finally:
        barrier.set()
        await asyncio.sleep(0)

    assert queued_flights <= budget, (
        f"{queued_flights} flights survived their cancelled schedulers against a budget of {budget}"
    )


@pytest.mark.asyncio
async def test_a_stale_queued_body_cannot_drop_a_reclaimed_thread() -> None:
    """A body that outlived a `clear_room` must not release the claim a later attempt now holds."""
    registry = ThreadRepairRegistry()
    thread_key = ("@agent:localhost", ROOM_ID, "$thread")
    flight_key = _flight_key("$thread", hydrate_sidecars=False)
    barrier = asyncio.Event()

    def queued_behind_barrier[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def runner() -> T:
            await barrier.wait()
            return await repair_factory()

        return asyncio.create_task(runner())

    async def scan() -> str:
        return "scanned"

    assert registry.reserve_speculative_repair(thread_key) is not None
    stale = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=queued_behind_barrier,
            repair=scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
        ),
    )
    await asyncio.sleep(0)
    assert flight_key in registry._tasks, "the flight never registered, so this proves nothing"

    registry.clear_room("@agent:localhost", ROOM_ID)
    reclaimed = registry.reserve_speculative_repair(thread_key)
    assert reclaimed is not None

    barrier.set()
    await asyncio.gather(stale, return_exceptions=True)
    await _drain_done_callbacks()

    surviving = registry._reserved_speculative.get(thread_key)
    assert surviving is not None, "the stale body released the reclaimed thread's newer claim"
    assert surviving.token is reclaimed


def _barrier_schedule[T](barrier: asyncio.Event) -> Callable[[Callable[[], Awaitable[T]]], asyncio.Task[T]]:
    """Queue a repair body behind a barrier, as the coordinator does."""

    def schedule(repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        async def runner() -> T:
            await barrier.wait()
            return await repair_factory()

        return asyncio.create_task(runner())

    return schedule


@pytest.mark.asyncio
async def test_a_scheduler_cleared_mid_preparation_cannot_hand_off_a_newer_claim() -> None:
    """A claim dropped at a membership boundary must not be usable by the caller that lost it.

    The original scheduler can still be inside pending-delta preparation when `clear_room` runs and
    a later attempt claims the same thread. Handing that newer claim to the stale flight would let a
    pre-clear scan answer a post-clear caller.
    """
    registry = ThreadRepairRegistry()
    thread_key = ("@agent:localhost", ROOM_ID, "$thread")
    flight_key = _flight_key("$thread", hydrate_sidecars=False)

    async def stale_scan() -> str:
        await asyncio.sleep(0.05)
        return "stale-result"

    async def fresh_scan() -> str:
        return "fresh-result"

    stale_token = registry.reserve_speculative_repair(thread_key)
    registry.clear_room("@agent:localhost", ROOM_ID)
    fresh_token = registry.reserve_speculative_repair(thread_key)
    assert stale_token is not None
    assert fresh_token is not None
    assert stale_token is not fresh_token

    stale = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=lambda factory: asyncio.create_task(factory()),
            repair=stale_scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
            claim_token=stale_token,
        ),
    )
    await asyncio.sleep(0)
    claim = registry._reserved_speculative.get(thread_key)
    assert claim is not None
    assert claim.token is fresh_token
    assert not claim.handed_off, "the stale scheduler handed off a claim it no longer owns"

    fresh = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=lambda factory: asyncio.create_task(factory()),
            repair=fresh_scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
            claim_token=fresh_token,
        ),
    )
    stale_result, fresh_result = await asyncio.gather(stale, fresh, return_exceptions=True)

    assert isinstance(stale_result, ThreadRepairSuppressedError)
    assert stale_result.reason == "claim_revoked"
    assert fresh_result == "fresh-result", "the post-clear caller was answered by the pre-clear flight"


@pytest.mark.asyncio
async def test_a_stale_body_cannot_erase_a_newer_flights_cleanup_token() -> None:
    """Cleanup metadata is per flight: a stale body must not strand the newer flight's claim."""
    registry = ThreadRepairRegistry()
    thread_key = ("@agent:localhost", ROOM_ID, "$thread")
    flight_key = _flight_key("$thread", hydrate_sidecars=False)
    stale_barrier = asyncio.Event()
    fresh_barrier = asyncio.Event()

    async def scan() -> str:
        return "scanned"

    stale_token = registry.reserve_speculative_repair(thread_key)
    stale = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=_barrier_schedule(stale_barrier),
            repair=scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
            claim_token=stale_token,
        ),
    )
    await asyncio.sleep(0)
    registry.clear_room("@agent:localhost", ROOM_ID)
    assert registry._flight_claims == {}, "clear_room left the detached flight's cleanup token behind"

    fresh_token = registry.reserve_speculative_repair(thread_key)
    fresh = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=_barrier_schedule(fresh_barrier),
            repair=scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
            claim_token=fresh_token,
        ),
    )
    await asyncio.sleep(0)
    assert registry._flight_claims.get(flight_key) is fresh_token

    stale_barrier.set()
    await asyncio.gather(stale, return_exceptions=True)
    await _drain_done_callbacks()
    assert registry._flight_claims.get(flight_key) is fresh_token, "the stale body erased newer cleanup metadata"

    # With its metadata intact, cancelling the newer flight before its body releases the claim.
    inner = registry._tasks.get(flight_key)
    assert inner is not None
    inner.cancel()
    await asyncio.gather(inner, fresh, return_exceptions=True)
    await _drain_done_callbacks()
    fresh_barrier.set()

    assert registry._reserved_speculative.get(thread_key) is None, "the newer claim was stranded"
    assert registry._flight_claims == {}


@pytest.mark.asyncio
async def test_clear_room_then_cancelling_the_old_flight_leaves_no_cleanup_state() -> None:
    """A detached flight cancelled before its body must leave nothing behind for the next attempt."""
    registry = ThreadRepairRegistry()
    thread_key = ("@agent:localhost", ROOM_ID, "$thread")
    flight_key = _flight_key("$thread", hydrate_sidecars=False)
    barrier = asyncio.Event()

    async def scan() -> str:
        return "scanned"

    token = registry.reserve_speculative_repair(thread_key)
    flight = asyncio.create_task(
        registry.run(
            flight_key,
            schedule=_barrier_schedule(barrier),
            repair=scan,
            result_arms_backoff=lambda _r: False,
            speculative=True,
            claim_token=token,
        ),
    )
    await asyncio.sleep(0)
    inner = registry._tasks.get(flight_key)
    assert inner is not None

    registry.clear_room("@agent:localhost", ROOM_ID)
    inner.cancel()
    await asyncio.gather(inner, flight, return_exceptions=True)
    await _drain_done_callbacks()
    barrier.set()

    assert registry._flight_claims == {}
    assert registry._reserved_speculative == {}
    assert registry.reserve_speculative_repair(thread_key) is not None
