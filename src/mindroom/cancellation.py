"""Task cancellation helpers shared across runtime and response paths."""

from __future__ import annotations

import asyncio
from typing import Any

_TASK_CANCEL_SOURCES: dict[asyncio.Task[Any], str] = {}


def _clear_task_cancel_source(task: asyncio.Task[Any]) -> None:
    """Drop recorded cancellation provenance once one task finishes."""
    _TASK_CANCEL_SOURCES.pop(task, None)


def request_task_cancel(task: asyncio.Task[Any], *, cancel_msg: str | None = None) -> None:
    """Cancel one task while preserving the first explicit cancellation source."""
    if cancel_msg is not None and task not in _TASK_CANCEL_SOURCES:
        _TASK_CANCEL_SOURCES[task] = cancel_msg
        task.add_done_callback(_clear_task_cancel_source)
    if cancel_msg is None:
        task.cancel()
    else:
        task.cancel(msg=cancel_msg)


def build_cancelled_error(reason: str | None) -> asyncio.CancelledError:
    """Return one CancelledError that preserves the task's in-flight cancel source."""
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        cancel_msg = _TASK_CANCEL_SOURCES.get(task)
        if cancel_msg is not None:
            return asyncio.CancelledError(cancel_msg)
    return asyncio.CancelledError(reason or "Run cancelled")
