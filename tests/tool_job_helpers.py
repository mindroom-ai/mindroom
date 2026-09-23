"""Shared tool job runtime construction for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_jobs.runtime import ToolJobRuntime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome


def _authorize_all(_job: BackgroundJob) -> bool:
    return True


async def _no_cleanup(_job: BackgroundJob) -> None:
    return None


def tool_job_runtime(
    storage_root: Path,
    *,
    authorize: Callable[[BackgroundJob], bool] = _authorize_all,
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] = _no_cleanup,
) -> ToolJobRuntime:
    """Build a runtime whose current grants allow every job and whose adapters need no extra cleanup."""
    return ToolJobRuntime(storage_root, authorize=authorize, cancel=cancel)


async def wait_for_status(runtime: ToolJobRuntime, job_id: str, status: str) -> None:
    """Wait until the runtime publishes the expected status for an accepted job."""
    entry = runtime._entries[job_id]
    while entry.job.status != status:
        await entry.changed.wait()
