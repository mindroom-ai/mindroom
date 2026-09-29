"""Tests for the advisory file-lock primitives."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import errno
import fcntl
import importlib.util
import os
import sys
import threading
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

import mindroom.file_locks
from mindroom.file_locks import (
    acquire_shared_file_lock,
    advisory_file_lock,
    async_exclusive_file_lock,
    file_lock_is_held,
    release_file_lock,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeMsvcrt(ModuleType):
    """Windows byte-range locking backed by flock, so separate handles still contend on this host."""

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        # msvcrt locks nbytes starting at the descriptor's current position.
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_UNLCK:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return
        assert mode == self.LK_NBLCK, "LK_LOCK gives up after about ten seconds, so waiters must poll"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # msvcrt reports a held range as EACCES, which OSError maps to PermissionError.
            raise OSError(errno.EACCES, "Permission denied") from None


@pytest.fixture
def windows_msvcrt() -> _FakeMsvcrt:
    """Record the msvcrt calls made by the Windows implementation."""
    return _FakeMsvcrt()


@pytest.fixture
def windows_file_locks(monkeypatch: pytest.MonkeyPatch, windows_msvcrt: _FakeMsvcrt) -> ModuleType:
    """Load a separate copy of the lock module the way Windows imports it: without fcntl."""
    spec = importlib.util.spec_from_file_location("windows_file_locks", mindroom.file_locks.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through the module registry.
    monkeypatch.setitem(sys.modules, spec.name, module)
    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        windows.setitem(sys.modules, "fcntl", None)
        windows.setitem(sys.modules, "msvcrt", windows_msvcrt)
        spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["posix", "windows"])
def file_locks(request: pytest.FixtureRequest) -> ModuleType:
    """Run a behavior test against the native module and the Windows implementation."""
    if request.param == "posix":
        return mindroom.file_locks
    return request.getfixturevalue("windows_file_locks")


@pytest.fixture
def flock_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every flock operation while still taking the real lock."""
    calls: list[int] = []
    real_flock = fcntl.flock

    def recording_flock(fd: int, operation: int) -> None:
        calls.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", recording_flock)
    return calls


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_serializes_in_process(tmp_path: Path, file_locks: ModuleType) -> None:
    # Separate async_exclusive_file_lock calls open distinct descriptions; the
    # lock contends across them, so only one critical section runs at a time.
    lock_path = tmp_path / "index.lock"
    active = 0
    max_active = 0

    async def worker() -> None:
        nonlocal active, max_active
        async with file_locks.async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(3)))

    assert max_active == 1


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_released_on_cancellation(tmp_path: Path, file_locks: ModuleType) -> None:
    lock_path = tmp_path / "index.lock"
    holding = asyncio.Event()

    async def holder() -> None:
        async with file_locks.async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            holding.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    await holding.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def acquire_once() -> bool:
        async with file_locks.async_exclusive_file_lock(lock_path, poll_seconds=0.01):
            return True

    # A cancelled waiter/holder must release the lock, so this acquires without hanging.
    assert await asyncio.wait_for(acquire_once(), timeout=2)


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_outlives_parent_handle_when_fd_is_inherited(tmp_path: Path) -> None:
    lock_path = tmp_path / "index.lock"

    async with async_exclusive_file_lock(
        lock_path,
        poll_seconds=0.01,
        retain_for_inherited_fds=True,
    ) as lock_file:
        inherited_fd = os.dup(lock_file.fileno())

    try:
        assert file_lock_is_held(lock_path) is True
    finally:
        os.close(inherited_fd)

    assert file_lock_is_held(lock_path) is False


@pytest.mark.asyncio
async def test_async_exclusive_file_lock_releases_inherited_fd_by_default(tmp_path: Path) -> None:
    lock_path = tmp_path / "index.lock"

    async with async_exclusive_file_lock(lock_path, poll_seconds=0.01) as lock_file:
        inherited_fd = os.dup(lock_file.fileno())

    try:
        assert file_lock_is_held(lock_path) is False
    finally:
        os.close(inherited_fd)


def test_advisory_file_lock_exclusive_blocks_second_holder(tmp_path: Path, file_locks: ModuleType) -> None:
    lock_path = tmp_path / "state.lock"
    order: list[str] = []
    second_attempting = threading.Event()

    def second() -> None:
        second_attempting.set()
        with file_locks.advisory_file_lock(lock_path):
            order.append("second-acquire")

    thread = threading.Thread(target=second)
    with file_locks.advisory_file_lock(lock_path):
        order.append("first-acquire")
        thread.start()
        assert second_attempting.wait(timeout=2)
        assert order == ["first-acquire"]
        order.append("first-release")

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert order == ["first-acquire", "first-release", "second-acquire"]


def test_advisory_file_lock_shared_allows_concurrent_readers(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    barrier = threading.Barrier(2)

    def reader() -> None:
        with advisory_file_lock(lock_path, exclusive=False):
            # Both readers must hold the shared lock simultaneously; if shared locks
            # excluded each other this barrier would time out and break.
            barrier.wait(timeout=2)

    threads = [threading.Thread(target=reader), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert barrier.broken is False


def test_file_lock_is_held_sees_another_holder(tmp_path: Path, file_locks: ModuleType) -> None:
    lock_path = tmp_path / "state.lock"

    with file_locks.advisory_file_lock(lock_path):
        assert file_locks.file_lock_is_held(lock_path) is True

    assert file_locks.file_lock_is_held(lock_path) is False


def test_posix_locks_keep_their_flock_calls(tmp_path: Path, flock_calls: list[int]) -> None:
    lock_path = tmp_path / "state.lock"

    with advisory_file_lock(lock_path):
        pass
    with advisory_file_lock(lock_path, exclusive=False):
        pass
    assert file_lock_is_held(lock_path) is False
    release_file_lock(acquire_shared_file_lock(lock_path))

    assert flock_calls == [
        fcntl.LOCK_EX,
        fcntl.LOCK_UN,
        fcntl.LOCK_SH,
        fcntl.LOCK_UN,
        fcntl.LOCK_EX | fcntl.LOCK_NB,
        fcntl.LOCK_UN,
        fcntl.LOCK_SH,
        fcntl.LOCK_UN,
    ]


@pytest.mark.asyncio
async def test_posix_async_lock_keeps_its_flock_calls(tmp_path: Path, flock_calls: list[int]) -> None:
    lock_path = tmp_path / "index.lock"

    async with async_exclusive_file_lock(lock_path, poll_seconds=0.01):
        assert file_lock_is_held(lock_path) is True
    async with async_exclusive_file_lock(lock_path, poll_seconds=0.01, retain_for_inherited_fds=True):
        pass

    assert flock_calls == [
        fcntl.LOCK_EX | fcntl.LOCK_NB,
        # The probe finds the lock held and leaves it alone.
        fcntl.LOCK_EX | fcntl.LOCK_NB,
        fcntl.LOCK_UN,
        # Retention closes the handle without unlocking it.
        fcntl.LOCK_EX | fcntl.LOCK_NB,
    ]


@pytest.mark.asyncio
async def test_windows_locks_cover_byte_zero_whatever_the_file_position(
    tmp_path: Path,
    windows_file_locks: ModuleType,
    windows_msvcrt: _FakeMsvcrt,
) -> None:
    # Append mode starts at the end of the file; msvcrt would lock a different byte there.
    lock_path = tmp_path / "state.lock"
    lock_path.write_text("left by an older writer", encoding="utf-8")

    with windows_file_locks.advisory_file_lock(lock_path):
        pass
    with windows_file_locks.advisory_file_lock(lock_path, exclusive=False):
        pass
    assert windows_file_locks.file_lock_is_held(lock_path) is False
    async with windows_file_locks.async_exclusive_file_lock(lock_path, poll_seconds=0.01):
        pass
    windows_file_locks.release_file_lock(windows_file_locks.acquire_shared_file_lock(lock_path))

    lock, unlock = (_FakeMsvcrt.LK_NBLCK, 1, 0), (_FakeMsvcrt.LK_UNLCK, 1, 0)
    assert windows_msvcrt.calls == [lock, unlock] * 5


def test_windows_shared_requests_lock_exclusively(tmp_path: Path, windows_file_locks: ModuleType) -> None:
    # msvcrt has no shared mode, so a second shared holder waits for the first.
    lock_path = tmp_path / "state.lock"
    order: list[str] = []
    second_attempting = threading.Event()

    def second() -> None:
        second_attempting.set()
        with windows_file_locks.advisory_file_lock(lock_path, exclusive=False):
            order.append("second-acquire")

    thread = threading.Thread(target=second)
    with windows_file_locks.advisory_file_lock(lock_path, exclusive=False):
        thread.start()
        assert second_attempting.wait(timeout=2)
        assert windows_file_locks.file_lock_is_held(lock_path) is True
        order.append("first-release")

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert order == ["first-release", "second-acquire"]
