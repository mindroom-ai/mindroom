"""Advisory file-lock helpers."""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path
    from typing import TextIO

_DEFAULT_POLL_SECONDS = 0.1

__all__ = [
    "acquire_shared_file_lock",
    "advisory_file_lock",
    "async_exclusive_file_lock",
    "file_lock_is_held",
    "release_file_lock",
]


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
) -> AsyncIterator[None]:
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
        yield
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
