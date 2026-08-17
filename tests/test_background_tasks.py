"""Tests for cancellation-safe background task draining."""

from __future__ import annotations

import asyncio

import pytest

from mindroom.background_tasks import wait_for_future_until_complete


@pytest.mark.asyncio
async def test_wait_for_future_returns_normal_result() -> None:
    """Return an accepted future's ordinary result unchanged."""
    future = asyncio.get_running_loop().create_future()
    future.set_result("done")

    assert await wait_for_future_until_complete(future) == "done"


@pytest.mark.asyncio
async def test_wait_for_future_propagates_worker_exception() -> None:
    """Propagate a worker failure when the waiter was not cancelled."""
    future = asyncio.get_running_loop().create_future()
    future.set_exception(ValueError("failed"))

    with pytest.raises(ValueError, match="failed"):
        await wait_for_future_until_complete(future)


@pytest.mark.asyncio
@pytest.mark.parametrize("chain_cancelled_result", [False, True])
async def test_wait_for_future_drains_cancelled_worker(
    chain_cancelled_result: bool,
) -> None:
    """Drain worker cancellation and preserve the selected cause policy."""
    future = asyncio.get_running_loop().create_future()
    cancellations: list[str] = []
    waiter = asyncio.create_task(
        wait_for_future_until_complete(
            future,
            on_cancel=lambda: cancellations.append("cancelled"),
            chain_cancelled_result=chain_cancelled_result,
        ),
    )

    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    future.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await waiter

    assert cancellations == ["cancelled"]
    if chain_cancelled_result:
        assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    else:
        assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_wait_for_future_preserves_shutdown_signal_while_draining_cancellation() -> None:
    """Do not replace a drained shutdown signal with the waiter's cancellation."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    current_task = asyncio.current_task()
    assert current_task is not None
    loop.call_later(0.01, future.set_exception, SystemExit("stop"))
    current_task.cancel()

    try:
        await wait_for_future_until_complete(future)
    except BaseException as exc:
        observed = exc
    else:
        pytest.fail("Expected the shutdown signal to propagate")

    assert isinstance(observed, SystemExit)
