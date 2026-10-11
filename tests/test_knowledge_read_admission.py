"""Reader admission shares capacity without blocking async callers or executor work."""

from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier, BoundedSemaphore, Event, Lock, get_ident
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


async def _prepare_request() -> ReadRequest:
    return ReadRequest("unused", "published")


class _Child:
    """Gate successful work and reaping independently to expose early slot release."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.work = asyncio.Event()
        self.reap = asyncio.Event()
        self.reap.set()
        self.killed = asyncio.Event()
        self.reaped = False

    def kill(self) -> None:
        self.killed.set()
        self.returncode = -9

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, None]:
        if data is not None:
            await self.work.wait()
            self.returncode = 0
        await self.reap.wait()
        self.reaped = True
        return _RESULT, None


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[_Child]:
    """Replace only OS process creation; exercise real admission, deadline and cleanup."""
    processes: list[_Child] = []

    async def spawn(*_args: object, **_kwargs: object) -> _Child:
        child = _Child()
        processes.append(child)
        assert sum(not process.reaped for process in processes) <= 4
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
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
            await _wait_until(lambda: prepared == 4)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), 1) == "available"
            assert len(children) == 4
            assert not any(task.done() for task in tasks)
            with pytest.raises(RuntimeError, match="Knowledge reader is busy"):
                read_process.read_chroma(ReadRequest("unused", "published"))
            for child in children:
                child.reap.clear()
                child.work.set()
            await asyncio.sleep(0.03)
            assert len(children) == 4, "Completed work must retain capacity until reaped"
            for child in children:
                child.reap.set()
            await _wait_until(lambda: len(children) == 8)
            for child in children:
                child.work.set()
            results = await asyncio.gather(*tasks)
            assert all(result.exists for result in results)
            assert all(child.reaped for child in children)
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
            children[0].work.set()
            with pytest.raises(TimeoutError, match="Knowledge read timed out"):
                await task
            assert not release_preparation.is_set()
            assert children[0].reaped
        finally:
            release_slot.cancel()
            release_work.cancel()
            release_preparation.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    _assert_all_slots_available()


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True])
async def test_interrupted_startup_keeps_capacity_until_child_reaped(  # noqa: PLR0915 - Keep lifecycle ordering visible.
    monkeypatch: pytest.MonkeyPatch,
    expire: bool,
) -> None:
    """A canceled startup remains owned through repeated cancellation and delayed reaping."""
    child = _Child()
    child.reap.clear()
    started = asyncio.Event()
    finish_startup = asyncio.Event()
    prepared = False
    spawns = 0

    async def spawn(*_args: object, **_kwargs: object) -> _Child:
        nonlocal spawns
        spawns += 1
        if spawns == 1:
            started.set()
            await finish_startup.wait()
            return child
        replacement = _Child()
        replacement.work.set()
        return replacement

    async def prepare() -> ReadRequest:
        nonlocal prepared
        prepared = True
        return await _prepare_request()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with ExitStack() as holders:
        for _ in range(3):
            holders.enter_context(read_process._read_slot())
        task = asyncio.create_task(read_process.read_chroma_async(prepare, timeout=0.03 if expire else 30))
        replacement = None
        try:
            await started.wait()
            if expire:
                await _wait_until(lambda: task.cancelling() > 0)
            else:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Interrupted startup must settle before caller exits"
            replacement = asyncio.create_task(read_process.read_chroma_async(_prepare_request))
            finish_startup.set()
            await asyncio.wait_for(child.killed.wait(), 1)
            if not expire:
                task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done()
            assert spawns == 1
            assert not prepared
            child.reap.set()
            if expire:
                with pytest.raises(TimeoutError, match="Knowledge read timed out"):
                    await task
            else:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert (await replacement).exists
            assert child.reaped
        finally:
            finish_startup.set()
            child.reap.set()
            task.cancel()
            if replacement is not None:
                replacement.cancel()
                await asyncio.gather(replacement, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)
    _assert_all_slots_available()


@pytest.mark.asyncio
async def test_startup_failure_releases_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed spawn returns its original error and leaves all four slots reusable."""

    async def spawn(*_args: object, **_kwargs: object) -> _Child:
        message = "synthetic spawn failure"
        raise OSError(message)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        await read_process.read_chroma_async(_prepare_request)
    _assert_all_slots_available()


def test_sync_and_separate_async_loops_share_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent loops wait behind four sync readers and reuse one global limit."""
    release_sync = Event()
    sync_started = Barrier(5)
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

    def sync_spawn(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        sync_started.wait(timeout=3)
        assert release_sync.wait(3)
        return subprocess.CompletedProcess(command, 0, stdout=_RESULT)

    async def spawn(*_args: object, **_kwargs: object) -> _Child:
        child = _Child()
        child.work.set()
        children.append(child)
        return child

    async def read() -> bool:
        return (await read_process.read_chroma_async(_prepare_request)).exists

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(read_process, "_read_slots", ObservedSlots(4))
    monkeypatch.setattr(subprocess, "run", sync_spawn)
    with ThreadPoolExecutor(max_workers=6) as executor:
        sync_reads = [executor.submit(read_process.read_chroma, ReadRequest("unused", "published")) for _ in range(4)]
        try:
            sync_started.wait(timeout=3)
            async_reads = [executor.submit(asyncio.run, read()) for _ in range(2)]
            assert async_waiting.wait(3)
            assert children == [], "Neither independent loop may bypass occupied sync capacity"
        finally:
            release_sync.set()
        assert all(future.result(timeout=3).exists for future in sync_reads)
        assert all(future.result(timeout=3) for future in async_reads)
    assert len(children) == 2
    assert all(child.reaped for child in children)
    assert asyncio.run(read())
    _assert_all_slots_available()
