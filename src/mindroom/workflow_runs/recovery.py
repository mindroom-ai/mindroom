"""What happens to workflow runs after their attempts ended without them: a restart, a reload, a lost worker."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.script_runs.reasons import (
    AGENT_ISOLATION_CHANGED,
    MAX_RUNTIME_EXCEEDED,
    PLUGIN_TOOLS_CHANGED,
    RUNTIME_RESTARTED,
    RUNTIME_SHUTDOWN,
    SUPERVISOR_UNAVAILABLE,
    WORKER_CONFIGURATION_CHANGED,
)
from mindroom.workflow_runs.models import WorkflowRunState

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from mindroom.workflow_runs.engine import ResumeAccess, WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowRun

# The reasons an attempt ends for that say nothing about its run: the run gets its next attempt on its own.
AUTO_RESUME_REASONS = frozenset(
    {
        RUNTIME_RESTARTED,
        RUNTIME_SHUTDOWN,
        PLUGIN_TOOLS_CHANGED,
        WORKER_CONFIGURATION_CHANGED,
        AGENT_ISOLATION_CHANGED,
        SUPERVISOR_UNAVAILABLE,
        MAX_RUNTIME_EXCEEDED,
    },
)


_JOB_ENDED = "The run's job ended before its next attempt started."


async def recover_runs(
    engine: WorkflowEngine,
    access: Callable[[WorkflowRun], ResumeAccess],
    live_jobs: Collection[str],
    waiting_jobs: Collection[str] = (),
) -> list[str]:
    """Settle the runs whose attempts ended and return the runs that got their next attempt.

    Called after the script runtime recorded which attempts ended, with the IDs of the jobs the job runtime holds
    unfinished. Every admitted run goes on under its saved owner, as *access* answers for it; its job only receives
    the report. A run whose job has ended was stopped with it, and an intent whose job was never admitted is deleted.
    A job of *waiting_jobs* was stopped by a restart and is not continued yet: its run is left alone until the job
    runtime granted the job in full and can cancel the run through it, and the continuation then goes on with it.
    """

    def access_of(run: WorkflowRun) -> ResumeAccess:
        first = run.state is WorkflowRunState.STARTING and run.attempt == 0
        if run.job_id is None:
            # A call that waits for its run itself launches what it reserved in this process.
            return None if first and run.created_at >= engine.started_at else access(run)
        if run.job_id in waiting_jobs:
            return "pending"
        # A first attempt is its job's to launch.
        return None if first or run.job_id not in live_jobs else access(run)

    for run in await asyncio.to_thread(engine.store.list_runs, include_finished=False):
        if run.state is not WorkflowRunState.STARTING or run.job_id is None or run.job_id in live_jobs:
            continue
        if run.attempt:
            # Nothing will start the attempt it waits for, and nothing else ends a run with no process.
            await engine.cancel(run.run_id, reason=_JOB_ENDED)
        elif run.created_at < engine.started_at:
            await asyncio.to_thread(engine.store.delete_intent, run.run_id)
    return await engine.reconcile(access_of)
