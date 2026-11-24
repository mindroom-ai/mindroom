"""Tests for cancellation-safe background task draining and supervised loops."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mindroom import background_tasks
from mindroom.background_tasks import (
    SupervisedLoop,
    create_background_task,
    drain_tasks,
    retry_delay_seconds,
    wait_for_future_until_complete,
)


@pytest.mark.asyncio
async def test_cancelling_a_drain_leaves_its_tasks_running() -> None:
    """Cancelling a drain stops only the wait, so a task it waited for still finishes."""
    release = asyncio.Event()

    async def settle() -> str:
        await release.wait()
        return "settled"

    task = asyncio.create_task(settle())
    drain = asyncio.create_task(drain_tasks({task}))
    await asyncio.sleep(0)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert not task.cancelled()
    release.set()
    assert await task == "settled"


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


_CALLER_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar("test_caller_owner", default=None)


@pytest.mark.asyncio
async def test_detached_background_task_does_not_retain_its_creator_context() -> None:
    """A task given its own context must not keep its creator's context alive until it finishes."""

    class _Owner:
        pass

    owner = _Owner()
    owner_ref = weakref.ref(owner)
    release = asyncio.Event()
    token = _CALLER_OWNER.set(owner)
    try:
        task = create_background_task(release.wait(), name="detached", context=contextvars.Context())
    finally:
        _CALLER_OWNER.reset(token)
    del owner
    gc.collect()

    try:
        assert owner_ref() is None
    finally:
        release.set()
        await task


@pytest.fixture
def restart_attempts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record each restart's backoff attempt and restart without waiting."""
    attempts: list[int] = []

    def record(attempt: int, **_delays: float) -> float:
        attempts.append(attempt)
        return 0.0

    monkeypatch.setattr(background_tasks, "retry_delay_seconds", record)
    return attempts


@pytest.fixture
def loop_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Capture what the supervisor logs."""
    logger = MagicMock()
    monkeypatch.setattr(background_tasks, "logger", logger)
    return logger


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [True, False])
async def test_supervised_loop_restarts_after_crash_or_return(
    crash: bool,
    restart_attempts: list[int],
    loop_logger: MagicMock,
) -> None:
    """A crashed loop logs the exception and an unexpectedly finished one a warning; both run again."""
    runs = 0
    restarted = asyncio.Event()

    async def run() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            if crash:
                msg = "loop failed"
                raise RuntimeError(msg)
            return
        restarted.set()
        await asyncio.Event().wait()

    loop = SupervisedLoop("test_loop", run)
    loop.start()
    try:
        await asyncio.wait_for(restarted.wait(), timeout=5)
    finally:
        await loop.stop()

    assert restart_attempts == [1]
    reported = loop_logger.exception if crash else loop_logger.warning
    reported.assert_called_once()
    assert reported.call_args.kwargs == {"task_name": "test_loop"}


@pytest.mark.asyncio
async def test_supervised_loop_start_is_idempotent_and_stop_cancels_and_drains() -> None:
    """A second start keeps the one running loop; stop waits for its cleanup and can repeat."""
    runs = 0
    started = asyncio.Event()
    cleaned_up = False

    async def run() -> None:
        nonlocal runs, cleaned_up
        runs += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned_up = True

    loop = SupervisedLoop("test_loop", run)
    loop.start()
    loop.start()
    await asyncio.wait_for(started.wait(), timeout=5)
    assert loop.running

    await loop.stop()

    assert cleaned_up
    assert not loop.running
    assert runs == 1
    await loop.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [True, False])
async def test_supervised_loop_does_not_restart_a_loop_that_ends_during_stop(
    crash: bool,
    restart_attempts: list[int],
    loop_logger: MagicMock,
) -> None:
    """A loop that turns the stop's cancellation into a return or an error stays stopped and unreported."""
    started = asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if crash:
                msg = "cleanup failed"
                raise RuntimeError(msg) from None

    loop = SupervisedLoop("test_loop", run)
    loop.start()
    await asyncio.wait_for(started.wait(), timeout=5)

    await loop.stop()

    assert restart_attempts == []
    loop_logger.exception.assert_not_called()
    loop_logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_supervised_loop_backoff_grows_and_resets_after_a_healthy_run(
    monkeypatch: pytest.MonkeyPatch,
    restart_attempts: list[int],
) -> None:
    """Quick consecutive crashes back off further each time; a run that lasted the healthy window starts over."""
    now = 0.0
    monkeypatch.setattr(background_tasks, "time", SimpleNamespace(monotonic=lambda: now))
    # Seconds each run lasts before it crashes; the fourth is a healthy run.
    durations = [0.0, 0.0, 0.0, background_tasks._LOOP_HEALTHY_RUN_SECONDS, 0.0]
    finished = asyncio.Event()

    async def run() -> None:
        nonlocal now
        if not durations:
            finished.set()
            await asyncio.Event().wait()
        now += durations.pop(0)
        msg = "loop failed"
        raise RuntimeError(msg)

    loop = SupervisedLoop("test_loop", run)
    loop.start()
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        await loop.stop()

    assert restart_attempts == [1, 2, 3, 1, 2]


def test_retry_delay_doubles_to_its_cap_and_stays_there_for_any_attempt() -> None:
    """A start retry blocked for days keeps getting the capped delay instead of overflowing."""
    delays = [
        retry_delay_seconds(attempt, initial_delay_seconds=2.0, max_delay_seconds=60.0) for attempt in (1, 2, 5, 6)
    ]

    assert delays == [2.0, 4.0, 32.0, 60.0]
    assert retry_delay_seconds(10_000, initial_delay_seconds=2.0, max_delay_seconds=60.0) == 60.0
