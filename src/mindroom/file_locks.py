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
