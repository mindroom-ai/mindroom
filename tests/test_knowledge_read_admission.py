"""Reader admission shares capacity without blocking async callers or executor work."""

from __future__ import annotations

import asyncio
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import BoundedSemaphore, Event, Lock, get_ident
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge import read_process
from mindroom.knowledge.read_protocol import ReadRequest

if TYPE_CHECKING:
    from collections.abc import Callable

_RESULT = b'{"exists":true,"documents":[],"error_type":null}'


async def _wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not condition():  # noqa: ASYNC110 - Observe cross-thread state without executor waiters.
            await asyncio.sleep(0.001)


def _wait_until_sync(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.001)


async def _prepare_request() -> ReadRequest:
    return ReadRequest("unused", "published")


class _Child:
    """Popen-shaped reader that gates its work and its reaping independently to expose early slot release."""

    def __init__(self, siblings: list[_Child], work: Event | None = None) -> None:
        self.siblings = siblings
        self.returncode: int | None = None
        self.served = Event()
        self.work = work or Event()
        self.reap = Event()
        self.reap.set()
        self.killed = Event()

    def kill(self) -> None:
        self.killed.set()

    def communicate(self, data: bytes | None = None, timeout: float | None = None) -> tuple[bytes, None]:
        assert timeout is None or timeout > 0
        if data is not None:
            self.served.set()
            assert len(_unreaped(self.siblings)) <= 4, "More native reads than reader slots"
            while not (self.work.wait(0.001) or self.killed.is_set()):
                pass
        return (b"" if self.killed.is_set() else _RESULT), None

    def __enter__(self) -> _Child:
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.reap.wait(5)
        self.returncode = -signal.SIGKILL if self.killed.is_set() else 0


def _serving(children: list[_Child]) -> list[_Child]:
    return [child for child in children if child.served.is_set()]


def _unreaped(children: list[_Child]) -> list[_Child]:
    return [child for child in _serving(children) if child.returncode is None]


def _spawner(children: list[_Child], work: Event | None = None) -> Callable[[], _Child]:
    def spawn() -> _Child:
        child = _Child(children, work)
        children.append(child)
        return child

    return spawn


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[_Child]:
    """Replace only OS process creation; exercise real admission, deadline and cleanup."""
    processes: list[_Child] = []
    monkeypatch.setattr(read_process, "_spawn_reader", _spawner(processes))
    return processes


def _assert_all_slots_available() -> None:
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(read_process._read_slot())
        with pytest.raises(RuntimeError, match="Knowledge reader is busy"), read_process._read_slot():
            pytest.fail("A fifth reader acquired capacity")


@pytest.mark.asyncio
async def test_async_burst_drains_after_reaping_and_leaves_executor_available(children: list[_Child]) -> None:
    """Eight callers finish under a four-unreaped-child cap, with no executor waiters."""
    prepared = 0

    async def prepare() -> ReadRequest:
        nonlocal prepared
        prepared += 1
        return await _prepare_request()

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        tasks = [asyncio.create_task(read_process.read_chroma_async(prepare)) for _ in range(8)]
        try:
            await _wait_until(lambda: len(_serving(children)) == 4)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), 1) == "available"
            assert prepared == 4
            assert not any(task.done() for task in tasks)
            with pytest.raises(RuntimeError, match="Knowledge reader is busy"):
                read_process.read_chroma(ReadRequest("unused", "published"))
            first = _serving(children)
            for child in first:
                child.reap.clear()
                child.work.set()
            await asyncio.sleep(0.03)
            assert len(_serving(children)) == 4, "Completed work must retain capacity until reaped"
            for child in first:
                child.reap.set()
            await _wait_until(lambda: len(_serving(children)) == 8)
            for child in children:
                child.work.set()
            results = await asyncio.gather(*tasks)
            assert all(result.exists for result in results)
            assert all(child.returncode == 0 for child in _serving(children))
        finally:
            for child in children:
                child.work.set()
                child.reap.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    _assert_all_slots_available()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_queued_exit_has_no_side_effects_or_permit_leak(children: list[_Child], cancel: bool) -> None:
    """Timed-out and canceled waiters neither spawn nor prepare, including release races."""
    prepared = False

    async def prepare() -> ReadRequest:
        nonlocal prepared
        prepared = True
        return await _prepare_request()

    with ExitStack() as holders:
        for _ in range(4):
            holders.enter_context(read_process._read_slot())
        task = asyncio.create_task(read_process.read_chroma_async(prepare, timeout=0.03 if not cancel else 30))
        await asyncio.sleep(0)
        if cancel:
            task.cancel()
            holders.close()  # Cancellation wins even when capacity becomes available immediately.
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError, match="Knowledge read timed out"):
                await task
            with pytest.raises(RuntimeError, match="Knowledge reader is busy"), read_process._read_slot():
                pytest.fail("Waiting timeout released somebody else's capacity")
        assert not prepared
        assert children == []
    _assert_all_slots_available()


@pytest.mark.asyncio
async def test_admission_and_preparation_share_original_deadline(children: list[_Child]) -> None:
    """Waiting consumes the read budget; preparation must not get a fresh timeout."""
    prepared = asyncio.Event()
    release_preparation = asyncio.Event()

    async def prepare() -> ReadRequest:
        prepared.set()
        await release_preparation.wait()
        return await _prepare_request()

    with ExitStack() as holders:
        for _ in range(4):
            holders.enter_context(read_process._read_slot())
        task = asyncio.create_task(read_process.read_chroma_async(prepare, timeout=0.5))
        loop = asyncio.get_running_loop()
        release_slot = loop.call_later(0.35, holders.close)
        release_work = loop.call_later(0.65, release_preparation.set)
        try:
            await asyncio.wait_for(prepared.wait(), 1)
            with pytest.raises(TimeoutError, match="Knowledge read timed out"):
                await task
            assert not release_preparation.is_set()
            [taken] = [child for child in children if child is not read_process._warm_reader]
            assert taken.killed.is_set()
            assert taken.returncode is not None
        finally:
            release_slot.cancel()
            release_work.cancel()
            release_preparation.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    _assert_all_slots_available()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_read", [False, True])
async def test_startup_failure_releases_capacity(monkeypatch: pytest.MonkeyPatch, async_read: bool) -> None:
    """A failed spawn returns its original error and leaves all four slots reusable."""

    def spawn() -> _Child:
        message = "synthetic spawn failure"
        raise OSError(message)

    monkeypatch.setattr(read_process, "_spawn_reader", spawn)
    read = (
        read_process.read_chroma_async(_prepare_request)
        if async_read
        else asyncio.to_thread(read_process.read_chroma, ReadRequest("unused", "published"))
    )
    with pytest.raises(OSError, match="synthetic spawn failure"):
        await read
    _assert_all_slots_available()


@pytest.mark.asyncio
async def test_saturated_sync_reads_leave_executor_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four blocked native reads must not let queued reads starve unrelated application work."""
    release = Event()
    children: list[_Child] = []
    monkeypatch.setattr(read_process, "_spawn_reader", _spawner(children, release))
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=6) as executor:
        loop.set_default_executor(executor)
        tasks = [
            asyncio.create_task(asyncio.to_thread(read_process.read_chroma, ReadRequest("unused", "published")))
            for _ in range(6)
        ]
        try:
            # Wait for the two rejections too, so no read can start after the blocked ones finish.
            await _wait_until(lambda: len(_serving(children)) == 4 and sum(task.done() for task in tasks) == 2)
            assert (
                await asyncio.wait_for(asyncio.to_thread(lambda: "unrelated work completed"), timeout=0.5)
                == "unrelated work completed"
            )
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, RuntimeError)]
    assert len(errors) == 2
    assert all("Knowledge reader is busy" in str(error) for error in errors)


def test_sync_and_separate_async_loops_share_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent loops wait behind four sync readers and reuse one global limit."""
    release_sync = Event()
    async_waiting = Event()
    waiting_threads: set[int] = set()
    children: list[_Child] = []
    lock = Lock()

    class ObservedSlots(BoundedSemaphore):
        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            acquired = super().acquire(blocking, timeout)
            if not acquired:
                with lock:
                    waiting_threads.add(get_ident())
                    if len(waiting_threads) == 2:
                        async_waiting.set()
            return acquired

    async def read() -> bool:
        return (await read_process.read_chroma_async(_prepare_request)).exists

    monkeypatch.setattr(read_process, "_spawn_reader", _spawner(children, release_sync))
    monkeypatch.setattr(read_process, "_read_slots", ObservedSlots(4))
    with ThreadPoolExecutor(max_workers=6) as executor:
        sync_reads = [executor.submit(read_process.read_chroma, ReadRequest("unused", "published")) for _ in range(4)]
        try:
            _wait_until_sync(lambda: len(_serving(children)) == 4)
            async_reads = [executor.submit(asyncio.run, read()) for _ in range(2)]
            assert async_waiting.wait(3)
            assert len(_serving(children)) == 4, "Neither independent loop may bypass occupied sync capacity"
        finally:
            release_sync.set()
        assert all(future.result(timeout=3).exists for future in sync_reads)
        assert all(future.result(timeout=3) for future in async_reads)
    assert len(_serving(children)) == 6
    assert all(child.returncode == 0 for child in _serving(children))
    assert asyncio.run(read())
    _assert_all_slots_available()
