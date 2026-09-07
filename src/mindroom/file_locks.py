"""Advisory file-lock helpers."""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path
    from typing import TextIO

_DEFAULT_POLL_SECONDS = 0.1

__all__ = [
    "InheritedFileLockCapability",
    "acquire_shared_file_lock",
    "advisory_file_lock",
    "async_exclusive_file_lock",
    "current_inherited_file_lock",
    "expose_inherited_file_lock",
    "file_lock_is_held",
    "release_file_lock",
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
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def acquire_shared_file_lock(lock_path: Path) -> TextIO:
    """Take a shared advisory lock and return the handle that keeps holding it.

    For claims that outlive a block: a process that has something open declares
    it for as long as that thing is open, and the operating system withdraws
    the claim if the process dies, which is what a PID file cannot do.
    """
    lock_file = _open_lock_file(lock_path)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
    except BaseException:
        lock_file.close()
        raise
    return lock_file


def release_file_lock(lock_file: TextIO) -> None:
    """Give up a lock taken by :func:`acquire_shared_file_lock`."""
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def file_lock_is_held(lock_path: Path) -> bool:
    """Return whether anyone currently holds this lock, without waiting to find out.

    A point-in-time answer: a holder can arrive the instant after it is read.
    Callers use it to refuse an operation that a holder would make unsafe, not
    to make the operation safe.
    """
    lock_file = _open_lock_file(lock_path)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                await asyncio.sleep(poll_seconds)
        yield lock_file
    finally:
        if acquired and not retain_for_inherited_fds:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        # With retention enabled, close without LOCK_UN: a subprocess may have
        # inherited this open-file description and must keep the lock held.
        lock_file.close()
