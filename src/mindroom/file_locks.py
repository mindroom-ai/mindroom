"""Advisory file-lock helpers."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path
    from typing import TextIO

_DEFAULT_POLL_SECONDS = 0.1

if sys.platform == "win32":
    import msvcrt

    # msvcrt locks a byte range starting at the current position, so every lock and
    # unlock seeks to byte 0 and covers exactly that byte. It has no shared mode:
    # shared requests lock exclusively, so concurrent shared holders wait for each other.

    def _try_lock_exclusive(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except PermissionError:  # EACCES: another handle holds the byte.
            return False
        return True

    def _lock(fd: int, *, exclusive: bool) -> None:  # noqa: ARG001
        # LK_LOCK raises after ten one-second retries, so an unbounded wait polls instead.
        while not _try_lock_exclusive(fd):
            time.sleep(_DEFAULT_POLL_SECONDS)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock_exclusive(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _lock(fd: int, *, exclusive: bool) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = [
    "InheritedFileLockCapability",
    "acquire_shared_file_lock",
    "advisory_file_lock",
    "async_exclusive_file_lock",
    "current_inherited_file_lock",
    "expose_inherited_file_lock",
    "file_lock_is_held",
    "release_file_lock",
    "wait_for_exclusive_lock",
]


@dataclass
class InheritedFileLockCapability:
    """A task-local authority to pass one still-owned lock into a subprocess."""

    scope: Path
    lock_file: TextIO
    active: bool = True

    def fileno_for(self, scope: Path) -> int | None:
        """Return the descriptor only while this capability owns the same scope."""
        if not self.active or self.lock_file.closed or self.scope != scope.resolve():
            return None
        return self.lock_file.fileno()


_inherited_file_lock: ContextVar[InheritedFileLockCapability | None] = ContextVar(
    "inherited_file_lock",
    default=None,
)


@contextmanager
def expose_inherited_file_lock(
    lock_file: TextIO,
    *,
    scope: Path,
) -> Iterator[InheritedFileLockCapability]:
    """Expose an owned lock to subprocess launchers in this task context."""
    capability = InheritedFileLockCapability(scope=scope.resolve(), lock_file=lock_file)
    token = _inherited_file_lock.set(capability)
    try:
        yield capability
    finally:
        capability.active = False
        _inherited_file_lock.reset(token)


def current_inherited_file_lock() -> InheritedFileLockCapability | None:
    """Return the task-local capability; callers must validate it at point of use."""
    return _inherited_file_lock.get()


def _open_lock_file(lock_path: Path) -> TextIO:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path.open("a", encoding="utf-8")


@contextmanager
def advisory_file_lock(lock_path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Acquire a blocking advisory file lock for synchronous code."""
    lock_file = _open_lock_file(lock_path)
    acquired = False
    try:
        _lock(lock_file.fileno(), exclusive=exclusive)
        acquired = True
        yield
    finally:
        if acquired:
            _unlock(lock_file.fileno())
        lock_file.close()


def acquire_shared_file_lock(lock_path: Path) -> TextIO:
    """Take a shared advisory lock and return the handle that keeps holding it.

    For claims that outlive a block: a process that has something open declares
    it for as long as that thing is open, and the operating system withdraws
    the claim if the process dies, which is what a PID file cannot do.
    """
    lock_file = _open_lock_file(lock_path)
    try:
        _lock(lock_file.fileno(), exclusive=False)
    except BaseException:
        lock_file.close()
        raise
    return lock_file


def release_file_lock(lock_file: TextIO) -> None:
    """Give up a lock taken by :func:`acquire_shared_file_lock`."""
    try:
        _unlock(lock_file.fileno())
    finally:
        lock_file.close()


def wait_for_exclusive_lock(descriptor: int, *, timeout_seconds: float) -> bool:
    """Lock an open descriptor exclusively, returning whether that succeeded within ``timeout_seconds``.

    For lock files that untrusted code can also open: a holder that never
    releases makes the caller fail instead of waiting forever. Closing the
    descriptor releases the lock.
    """
    deadline = time.monotonic() + timeout_seconds
    while not _try_lock_exclusive(descriptor):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_DEFAULT_POLL_SECONDS)
    return True


def file_lock_is_held(lock_path: Path) -> bool:
    """Return whether anyone currently holds this lock, without waiting to find out.

    A point-in-time answer: a holder can arrive the instant after it is read.
    Callers use it to refuse an operation that a holder would make unsafe, not
    to make the operation safe.
    """
    lock_file = _open_lock_file(lock_path)
    try:
        if not _try_lock_exclusive(lock_file.fileno()):
            return True
        _unlock(lock_file.fileno())
        return False
    finally:
        lock_file.close()


@asynccontextmanager
async def async_exclusive_file_lock(
    lock_path: Path,
    *,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    retain_for_inherited_fds: bool = False,
) -> AsyncIterator[TextIO]:
    """Acquire an exclusive advisory file lock without blocking the event loop."""
    lock_file = _open_lock_file(lock_path)
    acquired = False
    try:
        while not acquired:
            acquired = _try_lock_exclusive(lock_file.fileno())
            if not acquired:
                await asyncio.sleep(poll_seconds)
        yield lock_file
    finally:
        if acquired and not retain_for_inherited_fds:
            _unlock(lock_file.fileno())
        # With retention enabled, close without unlocking: on POSIX a subprocess may
        # have inherited this open-file description and must keep the lock held.
        lock_file.close()
