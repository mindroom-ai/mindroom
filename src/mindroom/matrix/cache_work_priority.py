"""Task-local priority marker for speculative startup Matrix cache work."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_STARTUP_CACHE_WORK = ContextVar("startup_cache_work", default=False)


@contextmanager
def startup_cache_work() -> Iterator[None]:
    """Mark cache work queued in this scope as cancellable startup work."""
    token = _STARTUP_CACHE_WORK.set(True)
    try:
        yield
    finally:
        _STARTUP_CACHE_WORK.reset(token)


def is_startup_cache_work() -> bool:
    """Return whether current task is running speculative startup cache work."""
    return _STARTUP_CACHE_WORK.get()
