"""Task cancellation helpers shared across runtime and response paths."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

CancelSource = Literal["user_stop", "sync_restart", "interrupted"]
USER_STOP_CANCEL_MSG = "user_stop"
SYNC_RESTART_CANCEL_MSG = "sync_restart"

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


def classify_cancel_source(exc: asyncio.CancelledError) -> CancelSource:
    """Return the visible cancellation provenance for one CancelledError."""
    if len(exc.args) == 0:
        return "interrupted"
    if exc.args[0] == USER_STOP_CANCEL_MSG:
        return "user_stop"
    if exc.args[0] == SYNC_RESTART_CANCEL_MSG:
        return "sync_restart"
    return "interrupted"


def build_cancelled_error(reason: str | None) -> asyncio.CancelledError:
    """Return one CancelledError that preserves the task's in-flight cancel source."""
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        cancel_msg = _TASK_CANCEL_SOURCES.get(task)
        if cancel_msg is not None:
            return asyncio.CancelledError(cancel_msg)
    return asyncio.CancelledError(reason or "Run cancelled")


def cancel_failure_reason(cancel_source: CancelSource) -> str:
    """Return the canonical failure reason for one cancellation provenance."""
    if cancel_source == "sync_restart":
        return "sync_restart_cancelled"
    if cancel_source == "user_stop":
        return "cancelled_by_user"
    return "interrupted"
