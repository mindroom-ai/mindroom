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


async def recover_runs(
    engine: WorkflowEngine,
    access: Callable[[WorkflowRun], ResumeAccess],
    live_jobs: Collection[str],
) -> list[str]:
    """Settle the runs whose attempts ended and return the runs that got their next attempt.

    Called after the script runtime recorded which attempts ended, with the IDs of the jobs the job runtime holds
    unfinished. A run goes on only while its job is one of them, since nobody else receives its report: an intent
    an earlier primary left without one is deleted, and an interrupted run without one ends as its attempt did.
    """

    def has_job(run: WorkflowRun) -> bool:
        return run.job_id in live_jobs or run.origin_job_id in live_jobs

    for run in await asyncio.to_thread(engine.store.list_runs, include_finished=False):
        intent = run.state is WorkflowRunState.STARTING and run.attempt == 0
        if intent and run.created_at < engine.started_at and not has_job(run):
            await asyncio.to_thread(engine.store.delete_intent, run.run_id)
    return await engine.reconcile(lambda run: access(run) if has_job(run) else None)
