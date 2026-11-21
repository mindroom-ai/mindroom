"""What happens to workflow runs after their attempts ended without them: a restart, a reload, a lost worker."""

from __future__ import annotations

import asyncio
from contextlib import suppress
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
from mindroom.workflow_runs.store import WorkflowRunNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

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


_JOB_STOPPED = "The run's job was stopped before its next attempt started."


async def recover_runs(
    engine: WorkflowEngine,
    access: Callable[[WorkflowRun], ResumeAccess],
    job_standing: Callable[[str], str],
) -> list[str]:
    """Settle the runs whose attempts ended and return the runs that got their next attempt.

    Called after the script runtime recorded which attempts ended. Every admitted run goes on under its saved owner,
    as *access* answers for it; its job only receives the report. *job_standing* says what the job runtime knows of
    a job at the moment its run is judged (`workflow_job_standing`), and only what it knows for sure decides:

    - A job that was stopped, by a Stop or a denial, ends its run, and a reservation whose job the runtime does not
      hold was never admitted and is deleted.
    - While the job runtime cannot be asked, the job waits to be continued, or is being stopped, the run gets no
      attempt and stays as it is. The continued job goes on with a run that waited for it (`engine.go_on`).
    - A job that ended in any other way, or that is gone, received or lost the report; the run goes on.
    """

    def access_of(run: WorkflowRun) -> ResumeAccess:
        # A run that names no attempt is its own call's or job's to launch first: a first attempt, or a resume's.
        own = run.state is WorkflowRunState.STARTING and run.script_run_id is None
        standing = "ended" if run.job_id is None else job_standing(run.job_id)
        if standing in ("unknown", "waiting", "stopping", "stopped"):
            return "pending"
        # Its own call, or the admission of its job, is still at work on it in this primary. A run whose job runs
        # is launched here too, once the job's own launch is over and left it starting.
        admitting = (standing == "absent" or run.job_id is None) and _recent(engine, run)
        return None if own and admitting else access(run)

    for listed in await asyncio.to_thread(engine.store.list_runs, include_finished=False):
        with suppress(WorkflowRunNotFoundError):
            run = engine.store.get_run(listed.run_id)
            standing = None if run.job_id is None else job_standing(run.job_id)
            if standing == "stopped":
                # Only while the run is still that job's: a resume may have given it to another one.
                await engine.cancel(run.run_id, reason=_JOB_STOPPED, job_id=run.job_id)
            elif standing == "absent" and run.attempt == 0 and not _recent(engine, run):
                await asyncio.to_thread(engine.store.delete_intent, run.run_id)
    return await engine.reconcile(access_of)


def _recent(engine: WorkflowEngine, run: WorkflowRun) -> bool:
    """Return whether *run* was reserved by this primary, whose own call or job admission is still at work on it."""
    return run.created_at >= engine.started_at
