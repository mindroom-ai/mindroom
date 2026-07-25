"""Deterministic single-flight, backoff, and retained-delta tests for thread repair."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.matrix.cache import ThreadCacheReplaceOutcome, thread_cache_rejection_reason
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.cache.thread_repair import ThreadRepairBackoffError, ThreadRepairRegistry
from mindroom.matrix.cache.write_coordinator import EventCacheWriteCoordinator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _schedule[T](repair_factory: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
    return asyncio.create_task(repair_factory())


def _event(event_id: str, timestamp: int, *, thread_id: str | None = None) -> dict[str, object]:
    content: dict[str, object] = {"body": event_id, "msgtype": "m.text"}
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": content,
    }


@pytest.mark.asyncio
async def test_twenty_missing_thread_callers_share_one_repair_and_converge(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """Concurrent readers and missing-cache appenders should install one ordered snapshot."""
    room_id = "!room:localhost"
    thread_id = "$thread"
    principal_id = "@agent:localhost"
    # Only the root owner closes the shared aiosqlite connection, whose thread is non-daemon.
    root_cache = SqliteEventCache(tmp_path / "event_cache.db")
    cache = root_cache.for_principal(principal_id)
    await cache.initialize()
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    repair_started = asyncio.Event()
    release_repair = asyncio.Event()
    participants_arrived = asyncio.Event()
    participant_count = 0
    fetch_count = 0
    expected_ids = (thread_id, "$initial", *(f"$live-{index}" for index in range(5)))

    async def repair() -> ThreadCacheReplaceOutcome:
        nonlocal fetch_count
        fetch_count += 1
        repair_started.set()
        await release_repair.wait()
        retained = coordinator.pending_thread_repair_deltas(
            room_id,
            thread_id,
            coordination_scope=principal_id,
        )
        events = [
            _event(thread_id, 1000),
            _event("$initial", 1500, thread_id=thread_id),
            *retained,
        ]
        return await cache.replace_thread_if_not_newer(
            room_id,
            thread_id,
            events,
            expected_membership_epoch=await cache.room_membership_epoch(room_id),
            fetch_started_at=time.time(),
        )

    async def read_or_repair() -> tuple[str, ...]:
        nonlocal participant_count
        participant_count += 1
        if participant_count == 20:
            participants_arrived.set()
        state = await cache.get_thread_cache_state(room_id, thread_id)
        rows = await cache.get_thread_events(room_id, thread_id)
        if rows is None or thread_cache_rejection_reason(state) is not None:
            await coordinator.run_thread_repair(
                room_id,
                thread_id,
                repair,
                coordination_scope=principal_id,
                result_is_usable=lambda outcome: outcome.usable,
            )
            coordinator.acknowledge_thread_repair_deltas(
                room_id,
                thread_id,
                expected_ids,
                coordination_scope=principal_id,
            )
        final_state = await cache.get_thread_cache_state(room_id, thread_id)
        final_rows = await cache.get_thread_events(room_id, thread_id)
        assert final_rows is not None
        assert thread_cache_rejection_reason(final_state) is None
        return tuple(str(event["event_id"]) for event in final_rows)

    async def append_during_repair(index: int) -> tuple[str, ...]:
        coordinator.retain_thread_repair_delta(
            room_id,
            thread_id,
            _event(f"$live-{index}", 2000 + index, thread_id=thread_id),
            coordination_scope=principal_id,
        )
        return await read_or_repair()

    owner = asyncio.create_task(read_or_repair())
    try:
        await repair_started.wait()
        callers = [
            *(asyncio.create_task(read_or_repair()) for _index in range(14)),
            *(asyncio.create_task(append_during_repair(index)) for index in range(5)),
        ]
        await participants_arrived.wait()
        release_repair.set()
        results = await asyncio.gather(owner, *callers)
        subsequent_result = await read_or_repair()
    finally:
        release_repair.set()
        await root_cache.close()

    assert fetch_count == 1
    assert all(result == expected_ids for result in results)
    assert subsequent_result == expected_ids


@pytest.mark.asyncio
async def test_repairs_are_principal_scoped_and_unrelated_threads_run_concurrently() -> None:
    """Identical Matrix IDs in separate principals must own separate mutable repair flights."""
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    release = asyncio.Event()
    all_started = asyncio.Event()
    started_keys: set[tuple[str, str]] = set()

    async def run(principal_id: str, thread_id: str) -> str:
        async def repair() -> str:
            started_keys.add((principal_id, thread_id))
            if len(started_keys) == 3:
                all_started.set()
            await release.wait()
            return f"{principal_id}:{thread_id}"

        repair_run = await coordinator.run_thread_repair(
            "!room:localhost",
            thread_id,
            repair,
            coordination_scope=principal_id,
            result_is_usable=lambda _result: True,
        )
        return repair_run.value

    tasks = [
        asyncio.create_task(run("@alice:localhost", "$same")),
        asyncio.create_task(run("@bob:localhost", "$same")),
        asyncio.create_task(run("@alice:localhost", "$other")),
    ]
    try:
        await all_started.wait()
        assert started_keys == {
            ("@alice:localhost", "$same"),
            ("@bob:localhost", "$same"),
            ("@alice:localhost", "$other"),
        }
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_repair_or_leak_ownership() -> None:
    """Caller cancellation should leave shared repair alive and ownership reusable."""
    registry = ThreadRepairRegistry()
    repair_started = asyncio.Event()
    release = asyncio.Event()
    repair_count = 0
    key = ("@agent:localhost", "!room:localhost", "$thread")

    async def repair() -> str:
        nonlocal repair_count
        repair_count += 1
        repair_started.set()
        await release.wait()
        return "usable"

    owner = asyncio.create_task(
        registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda _result: True,
        ),
    )
    await repair_started.wait()
    waiter = asyncio.create_task(
        registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda _result: True,
        ),
    )
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    joined = await waiter
    second = await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_is_usable=lambda _result: True,
    )

    assert joined.value == "usable"
    assert joined.joined is True
    assert second.value == "usable"
    assert second.joined is False
    assert repair_count == 2


@pytest.mark.asyncio
async def test_repair_bypasses_cancelled_room_fence_without_crossing_same_thread_work() -> None:
    """Read-style repair ownership must not deadlock behind a cancelled room fence."""
    coordinator = EventCacheWriteCoordinator(logger=MagicMock())
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()
    repair_started = asyncio.Event()

    async def blocking_sibling_update() -> None:
        blocker_started.set()
        await release_blocker.wait()

    async def cancelled_room_update() -> None:
        msg = "cancelled room update should not start"
        raise AssertionError(msg)

    async def repair() -> str:
        repair_started.set()
        return "usable"

    blocker = coordinator.queue_thread_update(
        "!room:localhost",
        "$sibling",
        blocking_sibling_update,
        name="matrix_cache_blocking_sibling",
        coordination_scope="@agent:localhost",
    )
    await blocker_started.wait()
    cancelled_room = coordinator.queue_room_update(
        "!room:localhost",
        cancelled_room_update,
        name="matrix_cache_cancelled_room",
        coordination_scope="@agent:localhost",
    )
    repair_task = asyncio.create_task(
        coordinator.run_thread_repair(
            "!room:localhost",
            "$thread",
            repair,
            coordination_scope="@agent:localhost",
            result_is_usable=lambda _result: True,
        ),
    )

    try:
        cancelled_room.cancel()
        await asyncio.gather(cancelled_room, return_exceptions=True)
        await asyncio.wait_for(repair_started.wait(), timeout=1.0)
        assert (await repair_task).value == "usable"
        assert blocker.done() is False
    finally:
        release_blocker.set()
        await asyncio.gather(blocker, repair_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failing_repair_enters_bounded_backoff_without_hot_retry() -> None:
    """A raising repair should suppress immediate repeated repairs."""
    now = 10.0
    registry = ThreadRepairRegistry(failure_backoff_seconds=2.0, clock=lambda: now)
    repair = AsyncMock(side_effect=RuntimeError("homeserver unavailable"))
    key = ("@agent:localhost", "!room:localhost", "$thread")

    with pytest.raises(RuntimeError, match="homeserver unavailable"):
        await registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda _result: True,
        )
    with pytest.raises(ThreadRepairBackoffError) as error:
        await registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda _result: True,
        )

    assert error.value.retry_after_seconds == 2.0
    repair.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ThreadCacheReplaceOutcome.HARD_FAILURE,
        ThreadCacheReplaceOutcome.WRITES_UNAVAILABLE,
    ],
)
async def test_unusable_repair_outcome_enters_backoff_without_reusing_history(
    outcome: ThreadCacheReplaceOutcome,
) -> None:
    """A failed install should throttle retries without caching reconstructed history."""
    now = 10.0
    registry = ThreadRepairRegistry(failure_backoff_seconds=2.0, clock=lambda: now)
    repair = AsyncMock(return_value=outcome)
    key = ("@agent:localhost", "!room:localhost", "$thread")

    first = await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_is_usable=lambda result: result.usable,
    )
    with pytest.raises(ThreadRepairBackoffError):
        await registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda result: result.usable,
        )

    assert first.value is outcome
    repair.assert_awaited_once()

    now = 12.0
    second = await registry.run(
        key,
        schedule=_schedule,
        repair=repair,
        result_is_usable=lambda result: result.usable,
    )

    assert second.value is outcome
    assert second.joined is False
    assert repair.await_count == 2


def test_retained_deltas_expire_once_any_new_scan_would_observe_them() -> None:
    """Unacknowledged deltas must not accumulate for threads that never install a snapshot."""
    now = 100.0
    registry = ThreadRepairRegistry(delta_retention_seconds=30.0, clock=lambda: now)
    key = ("@agent:localhost", "!room:localhost", "$thread")

    registry.retain_delta(key, _event("$old", 1000, thread_id="$thread"))
    now = 140.0
    registry.retain_delta(key, _event("$new", 2000, thread_id="$thread"))

    assert [source["event_id"] for source in registry.pending_deltas(key)] == ["$new"]


@pytest.mark.asyncio
async def test_retained_delta_survives_a_scan_that_outlives_the_retention_window() -> None:
    """A scan started before the event must still replay it, however long pagination takes."""
    now = 100.0
    registry = ThreadRepairRegistry(delta_retention_seconds=30.0, clock=lambda: now)
    key = ("@agent:localhost", "!room:localhost", "$thread")
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    replayed_event_ids: list[str] = []

    async def repair() -> str:
        scan_started.set()
        await release_scan.wait()
        replayed_event_ids.extend(str(source["event_id"]) for source in registry.pending_deltas(key))
        return "stored"

    flight = asyncio.create_task(
        registry.run(
            key,
            schedule=_schedule,
            repair=repair,
            result_is_usable=lambda _result: True,
        ),
    )
    try:
        await scan_started.wait()
        registry.retain_delta(key, _event("$live", 2000, thread_id="$thread"))
        # Advance beyond retention while the older scan still owns this key.
        now = 200.0
    finally:
        release_scan.set()
        await flight

    assert replayed_event_ids == ["$live"]
    now = 300.0
    assert registry.pending_deltas(key) == ()
