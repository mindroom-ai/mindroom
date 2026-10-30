"""Task cancellation helpers shared across runtime and response paths."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

TaskCancelSource = Literal["user_stop", "sync_restart"]
CancelSource = Literal["user_stop", "sync_restart", "interrupted"]
USER_STOP_CANCEL_MSG = "user_stop"
SYNC_RESTART_CANCEL_MSG = "sync_restart"

__all__ = [
    "SYNC_RESTART_CANCEL_MSG",
    "USER_STOP_CANCEL_MSG",
    "CancelSource",
    "TaskCancelSource",
    "cancel_failure_reason",
    "cancel_message_for_source",
    "classify_cancel_source",
    "current_task_is_process_shutdown",
    "request_task_cancel",
    "task_cancel_source_from_message",
    "task_is_process_shutdown",
]

_PROCESS_SHUTDOWN_TASKS: set[asyncio.Task[Any]] = set()


def cancel_message_for_source(cancel_source: TaskCancelSource | None) -> str | None:
    """Return asyncio task-cancel message for one typed source."""
    if cancel_source == "user_stop":
        return USER_STOP_CANCEL_MSG
    if cancel_source == "sync_restart":
        return SYNC_RESTART_CANCEL_MSG
    return None


def task_cancel_source_from_message(cancel_msg: str | None) -> TaskCancelSource | None:
    """Return typed task-cancel source for legacy cancel-message plumbing."""
    if cancel_msg == USER_STOP_CANCEL_MSG:
        return "user_stop"
    if cancel_msg == SYNC_RESTART_CANCEL_MSG:
        return "sync_restart"
    return None


def request_task_cancel(
    task: asyncio.Task[Any],
    *,
    cancel_source: TaskCancelSource | None = None,
    process_shutdown: bool = False,
) -> None:
    """Cancel one task, its cancellation carrying ``cancel_source`` as its message (``classify_cancel_source``).

    ``process_shutdown`` tags the task as handed to a successor runtime until it finishes.
    """
    if process_shutdown and task not in _PROCESS_SHUTDOWN_TASKS:
        _PROCESS_SHUTDOWN_TASKS.add(task)
        task.add_done_callback(_PROCESS_SHUTDOWN_TASKS.discard)
    task.cancel(msg=cancel_message_for_source(cancel_source))


def current_task_is_process_shutdown() -> bool:
    """Return whether a shutdown that hands this task's work to a successor runtime cancelled it.

    That is an orderly process shutdown or an entity replacement; either way
    the task leaves its turn pending for the successor to replay.
    """
    return task_is_process_shutdown(asyncio.current_task())


def task_is_process_shutdown(task: asyncio.Task[Any] | None) -> bool:
    """Read shutdown disposition from an explicitly retained response owner task."""
    return task is not None and task in _PROCESS_SHUTDOWN_TASKS


def classify_cancel_source(exc: asyncio.CancelledError) -> CancelSource:
    """Return the visible cancellation provenance for one CancelledError."""
    return task_cancel_source_from_message(exc.args[0] if exc.args else None) or "interrupted"


def cancel_failure_reason(cancel_source: CancelSource) -> str:
    """Return the canonical failure reason for one cancellation provenance."""
    if cancel_source == "sync_restart":
        return "sync_restart_cancelled"
    if cancel_source == "user_stop":
        return "cancelled_by_user"
    return "interrupted"
