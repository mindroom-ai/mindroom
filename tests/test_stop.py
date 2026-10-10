"""A reply span's Stop must hard-cancel the task that executes it, then its Agno run, while this instance runs it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.cancellation import USER_STOP_CANCEL_MSG
from mindroom.stop import SpanRegistry


@pytest.mark.asyncio
async def test_a_span_stopped_before_its_task_registers_is_cancelled_when_it_does() -> None:
    """A Stop between a span's claim and its task's registration cancels the task as it registers."""
    registry = SpanRegistry()
    registry.expect("span-1")
    assert not registry.cancel("span-1", cancel_source="user_stop")
    task = asyncio.create_task(asyncio.Event().wait())
    registry.register("span-1", task)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancel_for_a_span_this_instance_does_not_run_is_not_kept() -> None:
    """Cancels of spans this instance never claimed, or whose scope exited, leave nothing behind."""
    registry = SpanRegistry()
    assert not registry.cancel("elsewhere", cancel_source=None)
    registry.expect("span-1")
    registry.forget("span-1")
    assert not registry.cancel("span-1", cancel_source=None)
    task = asyncio.create_task(asyncio.sleep(0))
    registry.register("span-1", task)
    await task
    assert not task.cancelled()


def _registry_running(task: asyncio.Task[None], *, fallback_seconds: float) -> SpanRegistry:
    """Return a registry whose span ``span-1`` is executed by ``task`` on Agno run ``run-123``."""
    registry = SpanRegistry(graceful_cancel_fallback_seconds=fallback_seconds)
    registry.register("span-1", task)
    registry.update_run_id("span-1", "run-123")
    return registry


async def _drain_run_cleanup(registry: SpanRegistry) -> None:
    """Wait for the Agno run cleanups a Stop scheduled in the background."""
    await asyncio.gather(*tuple(registry._cleanup_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_stop_hard_cancels_when_the_run_never_becomes_cancellable() -> None:
    """A Stop must hard-cancel the span's task at once when its Agno run is not live yet."""
    started = asyncio.Event()
    completed = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def response_that_would_complete() -> None:
        try:
            started.set()
            await asyncio.sleep(0.1)
            completed.set()
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(response_that_would_complete())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=0.01)

    with patch("mindroom.stop.acancel_run", new=AsyncMock(return_value=False)):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)
        await _drain_run_cleanup(registry)

    with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
        await task
    assert not completed.is_set()


@pytest.mark.asyncio
async def test_a_stop_hard_cancels_when_agno_cancellation_fails() -> None:
    """Cancellation-manager failures must not disable the hard cancel."""
    started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(hung_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=0.01)

    with patch("mindroom.stop.acancel_run", new=AsyncMock(side_effect=RuntimeError("redis down"))):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)
        await _drain_run_cleanup(registry)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_stop_hard_cancels_without_waiting_for_agno_cancellation() -> None:
    """A successful Agno cleanup request must not delay the hard cancel."""
    started = asyncio.Event()
    allow_task_to_finish = asyncio.Event()
    cleanup_requested = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            await allow_task_to_finish.wait()
            raise

    async def graceful_cancel_run(_run_id: str) -> bool:
        cleanup_requested.set()
        allow_task_to_finish.set()
        return True

    task = asyncio.create_task(hung_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=1.0)

    with patch("mindroom.stop.acancel_run", new=graceful_cancel_run):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.1)
        await asyncio.wait_for(cleanup_requested.wait(), timeout=0.2)
        await _drain_run_cleanup(registry)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_slow_agno_cancellation_does_not_cancel_the_task_again() -> None:
    """A slow Agno cleanup call must not trigger a second hard cancel while the task unwinds."""
    started = asyncio.Event()
    allow_task_to_finish = asyncio.Event()
    task_cancelled = asyncio.Event()
    cancellation_manager_started = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            await allow_task_to_finish.wait()
            raise

    async def hanging_cancel_run(_run_id: str) -> bool:
        cancellation_manager_started.set()
        await asyncio.sleep(999)
        return True

    task = asyncio.create_task(hung_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=1.0)

    with patch("mindroom.stop.acancel_run", new=hanging_cancel_run):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.1)
        await asyncio.wait_for(cancellation_manager_started.wait(), timeout=0.2)
        await _drain_run_cleanup(registry)
        assert not task.done()
        allow_task_to_finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_stop_retries_until_the_run_becomes_cancellable() -> None:
    """The hard cancel is immediate even when Agno needs a retry before its cleanup succeeds."""
    started = asyncio.Event()
    allow_task_to_finish = asyncio.Event()
    hard_cancelled = asyncio.Event()
    task_cancelled = asyncio.Event()
    cancel_attempts: list[str] = []

    async def graceful_response() -> None:
        try:
            started.set()
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            hard_cancelled.set()
            await allow_task_to_finish.wait()
            task_cancelled.set()
            raise

    async def fake_acancel_run(run_id: str) -> bool:
        cancel_attempts.append(run_id)
        if len(cancel_attempts) == 1:
            return False
        allow_task_to_finish.set()
        return True

    task = asyncio.create_task(graceful_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=0.3)

    with patch("mindroom.stop.acancel_run", new=fake_acancel_run):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(hard_cancelled.wait(), timeout=0.1)
        await _drain_run_cleanup(registry)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)
    assert cancel_attempts == ["run-123", "run-123"]
    assert task_cancelled.is_set()


@pytest.mark.asyncio
async def test_a_stopped_span_cancels_the_run_its_retry_moves_to() -> None:
    """A span already stopped cancels the Agno run a retried attempt reports next."""
    started = asyncio.Event()
    allow_task_to_finish = asyncio.Event()
    hard_cancelled = asyncio.Event()
    first_cancel_attempt = asyncio.Event()
    second_cancel_attempt = asyncio.Event()
    cancel_attempts: list[str] = []

    async def graceful_response() -> None:
        try:
            started.set()
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            hard_cancelled.set()
            await allow_task_to_finish.wait()
            raise

    async def fake_acancel_run(run_id: str) -> bool:
        cancel_attempts.append(run_id)
        if run_id == "run-123":
            first_cancel_attempt.set()
        if run_id == "run-456":
            second_cancel_attempt.set()
            allow_task_to_finish.set()
        return True

    task = asyncio.create_task(graceful_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=0.3)

    with patch("mindroom.stop.acancel_run", new=fake_acancel_run):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        await asyncio.wait_for(hard_cancelled.wait(), timeout=0.1)
        await asyncio.wait_for(first_cancel_attempt.wait(), timeout=0.2)
        registry.update_run_id("span-1", "run-456")
        await asyncio.wait_for(second_cancel_attempt.wait(), timeout=0.2)
        await _drain_run_cleanup(registry)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)
    assert cancel_attempts == ["run-123", "run-456"]


@pytest.mark.asyncio
async def test_a_stop_cancels_the_run_after_its_task_finished() -> None:
    """The Agno run cleanup still runs when the span's task finishes before it."""
    started = asyncio.Event()

    async def short_lived_response() -> None:
        started.set()
        await asyncio.sleep(999)

    task = asyncio.create_task(short_lived_response())
    await started.wait()
    registry = _registry_running(task, fallback_seconds=0.1)

    cancel_run = AsyncMock(return_value=True)
    with patch("mindroom.stop.acancel_run", new=cancel_run):
        assert registry.cancel("span-1", cancel_source="user_stop") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain_run_cleanup(registry)

    cancel_run.assert_awaited_once_with("run-123")
