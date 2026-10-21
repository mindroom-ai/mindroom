"""A reply span's Stop must hard-cancel the task that executes it while this instance runs it."""

from __future__ import annotations

import asyncio

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


def _registry_running(task: asyncio.Task[None]) -> SpanRegistry:
    """Return a registry whose span ``span-1`` is executed by ``task`` on run ``run-123``."""
    registry = SpanRegistry()
    registry.register("span-1", task)
    registry.update_run_id("span-1", "run-123")
    return registry


@pytest.mark.asyncio
async def test_a_stop_hard_cancels_the_spans_task() -> None:
    """A Stop cancels the span's running task at once, with the user-stop cancel message."""
    started = asyncio.Event()
    completed = asyncio.Event()

    async def response_that_would_complete() -> None:
        started.set()
        await asyncio.Event().wait()
        completed.set()

    task = asyncio.create_task(response_that_would_complete())
    await asyncio.wait_for(started.wait(), timeout=1)
    registry = _registry_running(task)

    assert registry.cancel("span-1", cancel_source="user_stop") is True

    with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
        await task
    assert not completed.is_set()
    assert registry.live_span_ids() == frozenset()


@pytest.mark.asyncio
async def test_a_second_stop_does_not_cancel_the_unwinding_task_again() -> None:
    """A second Stop while the span's task unwinds from the first must not interrupt that unwinding."""
    started = asyncio.Event()
    unwinding = asyncio.Event()
    finish_unwinding = asyncio.Event()
    unwound = asyncio.Event()

    async def response_with_slow_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.wait_for(finish_unwinding.wait(), timeout=1)
            unwound.set()
            raise

    task = asyncio.create_task(response_with_slow_cleanup())
    await asyncio.wait_for(started.wait(), timeout=1)
    registry = _registry_running(task)
    assert registry.cancel("span-1", cancel_source="user_stop") is True
    await asyncio.wait_for(unwinding.wait(), timeout=1)

    assert registry.cancel("span-1", cancel_source="user_stop") is True
    await asyncio.sleep(0)
    assert not task.done()

    finish_unwinding.set()
    with pytest.raises(asyncio.CancelledError, match=USER_STOP_CANCEL_MSG):
        await task
    assert unwound.is_set()
