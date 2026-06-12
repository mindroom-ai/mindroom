"""One-shot retry of responses cancelled by sync-restart recovery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.response_runner import ResponseRequest, ResponseRunner
from mindroom.sync_restart_retry import SyncRestartRetryQueue
from tests.conftest import request_envelope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _request(on_sync_restart_cancelled: Callable[[], None] | None = None) -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="Hello",
        response_envelope=request_envelope(),
        on_sync_restart_cancelled=on_sync_restart_cancelled,
    )


def _cancelled_outcome(*, failure_reason: str, visible: bool = True) -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$interrupted_note" if visible else None,
        is_visible_response=visible,
        failure_reason=failure_reason,
    )


def _notify(
    runner: ResponseRunner,
    request: ResponseRequest,
    outcome: FinalDeliveryOutcome,
    *,
    delivery_cancelled: bool = True,
) -> None:
    runner._notify_sync_restart_cancelled(
        request,
        outcome,
        delivery_cancelled=delivery_cancelled,
        delivery_failure_reason=outcome.failure_reason,
    )


def test_notify_fires_for_marked_handled_sync_restart_cancellation() -> None:
    """A sync-restart cancellation that left a visible note must report itself."""
    calls: list[str] = []
    _notify(
        ResponseRunner(deps=MagicMock()),
        _request(on_sync_restart_cancelled=lambda: calls.append("retry")),
        _cancelled_outcome(failure_reason="sync_restart_cancelled"),
    )
    assert calls == ["retry"]


def test_notify_ignores_user_stop_and_unmarked_turns() -> None:
    """User stops and turns without a visible note must not request a retry."""
    calls: list[str] = []
    runner = ResponseRunner(deps=MagicMock())
    request = _request(on_sync_restart_cancelled=lambda: calls.append("retry"))

    _notify(runner, request, _cancelled_outcome(failure_reason="cancelled_by_user"))
    _notify(runner, request, _cancelled_outcome(failure_reason="sync_restart_cancelled", visible=False))
    _notify(runner, request, _cancelled_outcome(failure_reason="sync_restart_cancelled"), delivery_cancelled=False)

    assert calls == []


@pytest.mark.asyncio
async def test_queue_runs_each_retry_exactly_once() -> None:
    """Flushing must run queued retries once and refuse re-registration."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    async def retry() -> None:
        runs.append("ran")

    assert queue.register("$event", retry) is True
    assert queue.register("$event", retry) is False
    assert queue.has_pending

    await queue.flush()
    assert runs == ["ran"]
    assert not queue.has_pending

    # Already-attempted keys never requeue, so a second stall cannot loop.
    assert queue.register("$event", retry) is False
    await queue.flush()
    assert runs == ["ran"]


@pytest.mark.asyncio
async def test_queue_isolates_individual_retry_failures() -> None:
    """One failing retry must not block the others."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    async def failing() -> None:
        msg = "deliberate test error"
        raise RuntimeError(msg)

    async def succeeding() -> None:
        runs.append("ok")

    queue.register("$a", failing)
    queue.register("$b", succeeding)
    await queue.flush()
    assert runs == ["ok"]


@pytest.mark.asyncio
async def test_queue_flushes_in_registration_order() -> None:
    """Retries must run FIFO so older interrupted turns answer first."""
    queue = SyncRestartRetryQueue()
    runs: list[str] = []

    def make_retry(key: str) -> Callable[[], Awaitable[None]]:
        async def retry() -> None:
            runs.append(key)

        return retry

    for key in ("$first", "$second", "$third"):
        queue.register(key, make_retry(key))

    await queue.flush()
    assert runs == ["$first", "$second", "$third"]


@pytest.mark.asyncio
async def test_cancelled_flush_logs_in_flight_key_and_keeps_rest_pending() -> None:
    """Cancelling a flush mid-retry must log the lost key, propagate, and keep later retries queued."""
    queue = SyncRestartRetryQueue()
    started = asyncio.Event()

    async def hanging() -> None:
        started.set()
        await asyncio.Event().wait()

    async def later() -> None:
        pass

    queue.register("$in_flight", hanging)
    queue.register("$later", later)
    flush_task = asyncio.create_task(queue.flush())
    await started.wait()

    flush_task.cancel()
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await flush_task

    assert [entry["source_event_id"] for entry in logs if entry["event"] == "sync_restart_retry_cancelled"] == [
        "$in_flight",
    ]
    # The interrupted key was already promoted to attempted and never requeues.
    assert queue.register("$in_flight", hanging) is False
    # The untouched retry survives for the next healthy sync response.
    assert queue.has_pending


@pytest.mark.asyncio
async def test_watchdog_cancelled_response_is_redispatched_once_and_answers() -> None:
    """The dispatch/retry seam answers on the retry and never retries twice."""
    queue = SyncRestartRetryQueue()
    runner = ResponseRunner(deps=MagicMock())
    answers: list[str] = []
    attempts = 0

    async def execute_action() -> None:
        nonlocal attempts
        attempts += 1

        def register_retry() -> None:
            queue.register("$source", execute_action)

        if attempts == 1:
            # First attempt: cancelled mid-generation by stall recovery.
            _notify(
                runner,
                _request(on_sync_restart_cancelled=register_retry),
                _cancelled_outcome(failure_reason="sync_restart_cancelled"),
            )
        else:
            answers.append("pong")

    await execute_action()
    assert queue.has_pending
    assert answers == []

    await queue.flush()  # The sync loop reported a healthy response again.
    assert answers == ["pong"]
    assert attempts == 2

    await queue.flush()
    assert attempts == 2


@pytest.mark.asyncio
async def test_user_stopped_response_is_not_retried() -> None:
    """A user stop must leave the retry queue empty."""
    queue = SyncRestartRetryQueue()
    runner = ResponseRunner(deps=MagicMock())

    def register_retry() -> None:
        queue.register("$source", MagicMock())

    _notify(
        runner,
        _request(on_sync_restart_cancelled=register_retry),
        _cancelled_outcome(failure_reason="cancelled_by_user"),
    )

    assert not queue.has_pending
