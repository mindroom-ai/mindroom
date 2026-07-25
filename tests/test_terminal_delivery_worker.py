"""Durable terminal delivery worker: retry policy, ordering, and lifecycle."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.message_target import MessageTarget
from mindroom.terminal_delivery import (
    PendingTerminalDelivery,
    TerminalDeliveryAttempt,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    _reset_terminal_delivery_store_runtime,
)
from mindroom.terminal_delivery_worker import TerminalDeliveryWorker, TerminalDeliveryWorkerDeps

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


ROOM_ID = "!room:localhost"
_STATE_WAIT_TIMEOUT_SECONDS = 5.0


class _Clock:
    """Deterministic wall clock shared by store and worker."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_store_runtime() -> None:
    """Keep process-wide store state from leaking between tests."""
    _reset_terminal_delivery_store_runtime()
    yield
    _reset_terminal_delivery_store_runtime()


def _intent(*, room_id: str = ROOM_ID, source_event_id: str = "$source") -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        agent_name="helper",
        target=MessageTarget.resolve(room_id, None, source_event_id),
        target_event_id=f"{source_event_id}-placeholder",
        anchor_event_id=source_event_id,
        source_event_ids=(source_event_id,),
        outcome_kind="completed",
        body="final answer",
        correlation_id=f"corr-{source_event_id}",
        response_kind="ai",
    )


def _worker(
    store: TerminalDeliveryStore,
    attempt: Callable[[PendingTerminalDelivery], Awaitable[TerminalDeliveryAttempt]],
    *,
    clock: _Clock,
    max_concurrency: int = 4,
    max_attempts: int = 12,
    is_ready: Callable[[], bool] | None = None,
    poll_interval_seconds: float = 15.0,
) -> TerminalDeliveryWorker:
    return TerminalDeliveryWorker(
        TerminalDeliveryWorkerDeps(
            store=store,
            attempt=attempt,
            is_ready=is_ready or (lambda: True),
            logger=MagicMock(),
            wall_clock=clock,
            jitter=lambda: 1.0,
            poll_interval_seconds=poll_interval_seconds,
            max_concurrency=max_concurrency,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=8.0,
            max_attempts=max_attempts,
        ),
    )


def _store(tmp_path: Path, clock: _Clock) -> TerminalDeliveryStore:
    return TerminalDeliveryStore(agent_name="helper", base_path=tmp_path / "tracking", clock=clock)


async def _wait_for_state(store: TerminalDeliveryStore, delivery_id: str, state: str) -> None:
    """Wait until one durable record reaches an expected state."""
    async with asyncio.timeout(_STATE_WAIT_TIMEOUT_SECONDS):
        while True:
            item = store.get(delivery_id)
            if item is not None and item.state == state:
                return
            await asyncio.sleep(0.01)


class TestRecoveryConvergence:
    """A committed terminal outcome eventually becomes visible."""

    @pytest.mark.asyncio
    async def test_delivery_lands_once_recovery_finishes(self, tmp_path: Path) -> None:
        """Recovery blocking longer than the immediate budget still converges."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        recovery_ready = False
        attempts: list[str] = []

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            attempts.append(item.transaction_id)
            if not recovery_ready:
                return TerminalDeliveryAttempt.transient("sync_recovery:SendRetryError")
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock)

        await worker.drain_once()
        blocked = store.get(recorded.delivery_id)
        assert blocked is not None
        assert blocked.state == "retry_wait"
        assert blocked.last_error == "sync_recovery:SendRetryError"

        recovery_ready = True
        clock.advance(blocked.next_attempt_at - clock.now)
        await worker.drain_once()

        delivered = store.get(recorded.delivery_id)
        assert delivered is not None
        assert delivered.state == "delivered"
        # The same revision reuses one Matrix transaction ID, so an at-least-once
        # transport still has an exactly-once visible effect.
        assert len(set(attempts)) == 1

    @pytest.mark.asyncio
    async def test_backoff_grows_and_is_bounded(self, tmp_path: Path) -> None:
        """Transient failures back off exponentially up to the configured ceiling."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("rate_limited:M_LIMIT_EXCEEDED")

        worker = _worker(store, attempt, clock=clock)
        delays: list[float] = []
        for _round in range(5):
            await worker.drain_once()
            item = store.get(recorded.delivery_id)
            assert item is not None
            delays.append(item.next_attempt_at - clock.now)
            clock.advance(item.next_attempt_at - clock.now)

        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]

    @pytest.mark.asyncio
    async def test_server_retry_hint_raises_the_wait(self, tmp_path: Path) -> None:
        """A rate-limit hint from the homeserver is honoured over a shorter backoff."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("rate_limited:M_LIMIT_EXCEEDED", retry_after_seconds=5.0)

        await _worker(store, attempt, clock=clock).drain_once()

        item = store.get(recorded.delivery_id)
        assert item is not None
        assert item.next_attempt_at - clock.now == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_retry_budget_exhaustion_dead_letters(self, tmp_path: Path) -> None:
        """A row that can never land stops retrying and is loudly dead-lettered."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("network:ConnectionError")

        worker = _worker(store, attempt, clock=clock, max_attempts=3)
        for _round in range(3):
            await worker.drain_once()
            item = store.get(recorded.delivery_id)
            assert item is not None
            clock.advance(max(0.0, item.next_attempt_at - clock.now))

        settled = store.get(recorded.delivery_id)
        assert settled is not None
        assert settled.state == "dead_letter"
        assert settled.settled_reason == "retry_budget_exhausted:network:ConnectionError"


class TestClassification:
    """Transient versus permanent versus superseded outcomes."""

    @pytest.mark.asyncio
    async def test_permanent_failure_in_one_room_does_not_block_others(self, tmp_path: Path) -> None:
        """A forbidden room dead-letters while every other room still delivers."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        forbidden = store.record(_intent(room_id="!forbidden:localhost", source_event_id="$forbidden"))
        healthy = store.record(_intent(room_id="!healthy:localhost", source_event_id="$healthy"))
        assert forbidden is not None
        assert healthy is not None

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            if item.target.room_id == "!forbidden:localhost":
                return TerminalDeliveryAttempt.permanent("forbidden:M_FORBIDDEN")
            return TerminalDeliveryAttempt.delivered_now()

        await _worker(store, attempt, clock=clock).drain_once()

        forbidden_row = store.get(forbidden.delivery_id)
        healthy_row = store.get(healthy.delivery_id)
        assert forbidden_row is not None
        assert forbidden_row.state == "dead_letter"
        assert healthy_row is not None
        assert healthy_row.state == "delivered"

    @pytest.mark.asyncio
    async def test_missing_target_is_superseded_not_retried(self, tmp_path: Path) -> None:
        """A vanished visible target ends the retry instead of looping forever."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        attempt_count = 0

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal attempt_count
            attempt_count += 1
            return TerminalDeliveryAttempt.superseded("target_event_missing")

        worker = _worker(store, attempt, clock=clock)
        await worker.drain_once()
        clock.advance(600.0)
        await worker.drain_once()

        settled = store.get(recorded.delivery_id)
        assert settled is not None
        assert settled.state == "superseded"
        assert attempt_count == 1

    @pytest.mark.asyncio
    async def test_attempt_exception_is_treated_as_transient(self, tmp_path: Path) -> None:
        """An unexpected attempt error retries rather than losing the outcome."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            msg = "boom"
            raise RuntimeError(msg)

        await _worker(store, attempt, clock=clock).drain_once()

        item = store.get(recorded.delivery_id)
        assert item is not None
        assert item.state == "retry_wait"
        assert item.last_error == "attempt_exception"


class TestSchedulingLimits:
    """Bounded concurrency, per-room ordering, and starvation."""

    @pytest.mark.asyncio
    async def test_backlog_respects_concurrency_and_per_room_order(self, tmp_path: Path) -> None:
        """Fifty rows across ten rooms drain fully, bounded, and in per-room order."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        expected_order: dict[str, list[str]] = {}
        for room_index in range(10):
            room_id = f"!room{room_index}:localhost"
            expected_order[room_id] = []
            for item_index in range(5):
                source_event_id = f"$r{room_index}-i{item_index}"
                recorded = store.record(_intent(room_id=room_id, source_event_id=source_event_id))
                assert recorded is not None
                expected_order[room_id].append(recorded.delivery_id)
                # Distinct scheduling times make the claim order deterministic.
                clock.advance(0.001)

        in_flight = 0
        peak_in_flight = 0
        per_room_in_flight: Counter[str] = Counter()
        peak_per_room = 0
        completion_order: dict[str, list[str]] = {room_id: [] for room_id in expected_order}

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal in_flight, peak_in_flight, peak_per_room
            in_flight += 1
            per_room_in_flight[item.target.room_id] += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            peak_per_room = max(peak_per_room, per_room_in_flight[item.target.room_id])
            await asyncio.sleep(0)
            completion_order[item.target.room_id].append(item.delivery_id)
            per_room_in_flight[item.target.room_id] -= 1
            in_flight -= 1
            return TerminalDeliveryAttempt.delivered_now()

        attempted = await _worker(store, attempt, clock=clock, max_concurrency=4).drain_once()

        assert attempted == 50
        assert peak_in_flight <= 4
        assert peak_per_room == 1
        assert completion_order == expected_order
        assert store.backlog().unsettled_count == 0

    @pytest.mark.asyncio
    async def test_oldest_due_rows_are_attempted_first(self, tmp_path: Path) -> None:
        """Scheduling order is by due time, so nothing starves behind newer work."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        first = store.record(_intent(source_event_id="$first"))
        clock.advance(10.0)
        second = store.record(_intent(source_event_id="$second"))
        assert first is not None
        assert second is not None
        seen: list[str] = []

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            seen.append(item.delivery_id)
            return TerminalDeliveryAttempt.delivered_now()

        await _worker(store, attempt, clock=clock, max_concurrency=1).drain_once()

        assert seen == [first.delivery_id, second.delivery_id]


class TestLifecycle:
    """Worker start, wake, and shutdown ownership."""

    @pytest.mark.asyncio
    async def test_wake_drains_without_waiting_for_the_poll_interval(self, tmp_path: Path) -> None:
        """A recovery-ready wakeup delivers immediately instead of after the scan."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        delivered = asyncio.Event()

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            delivered.set()
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, poll_interval_seconds=600.0)
        worker.start()
        try:
            worker.wake(reason="sync_response_applied")
            await asyncio.wait_for(delivered.wait(), timeout=5.0)
            await _wait_for_state(store, recorded.delivery_id, "delivered")
        finally:
            await worker.stop()

        settled = store.get(recorded.delivery_id)
        assert settled is not None
        assert settled.state == "delivered"
        assert not worker.running

    @pytest.mark.asyncio
    async def test_shutdown_during_an_attempt_keeps_the_row_retryable(self, tmp_path: Path) -> None:
        """Stopping mid-attempt returns the lease instead of losing the outcome."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        started = asyncio.Event()

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            started.set()
            await asyncio.sleep(3600)
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, poll_interval_seconds=600.0)
        worker.start()
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await worker.stop()

        item = store.get(recorded.delivery_id)
        assert item is not None
        assert item.state == "retry_wait"
        assert item.last_error == "worker_shutdown"
        assert item.attempts == 0

    @pytest.mark.asyncio
    async def test_worker_stays_idle_until_the_runtime_is_ready(self, tmp_path: Path) -> None:
        """Delivery never runs before the bot has a live, synced Matrix client."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        assert store.record(_intent()) is not None
        attempted = 0
        ready = False

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal attempted
            attempted += 1
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, is_ready=lambda: ready, poll_interval_seconds=0.01)
        worker.start()
        try:
            await asyncio.sleep(0.05)
            assert attempted == 0
            ready = True
            worker.wake(reason="test_ready")
            await asyncio.sleep(0.1)
        finally:
            await worker.stop()

        assert attempted == 1

    @pytest.mark.asyncio
    async def test_stop_leaves_no_pending_worker_task(self, tmp_path: Path) -> None:
        """The worker owns exactly one task and retrieves its result on shutdown."""
        clock = _Clock()
        store = _store(tmp_path, clock)

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, poll_interval_seconds=600.0)
        worker.start()
        assert worker.running
        await worker.stop()

        worker_tasks = [task for task in asyncio.all_tasks() if task.get_name() == "terminal_delivery_worker"]
        assert worker_tasks == []
