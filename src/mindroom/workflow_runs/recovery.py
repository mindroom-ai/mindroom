"""What happens to workflow runs after their attempts ended without them: a restart, a reload, a lost worker."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
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

    from mindroom.workflow_runs.engine import WorkflowEngine

logger = get_logger(__name__)

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


async def recover_runs(engine: WorkflowEngine, job_standing: Callable[[str], str]) -> list[str]:
    """Settle the runs whose attempts ended and return the runs that got their next attempt here.

    Called after the script runtime recorded which attempts ended. An ended attempt is settled whatever else holds:
    its run ends as the attempt left it, or waits for its next attempt when an interruption stopped it. Who starts
    that attempt follows from what the job runtime knows of the run's job at this moment (`workflow_job_standing`):

    - While the job is unfinished, only its operation does (`engine.wait`), which a Stop of the job cancels. The
      same holds while the job runtime cannot be asked.
    - A job that was stopped, by a Stop or a denial, ends its run, and a reservation whose job the runtime does not
      hold was never admitted and is deleted.
    - A run with no job, or whose job ended in any other way or is gone, gets it here, under its saved owner as
      `engine.admit` allows. Not while this primary's own call or job admission is still at work on it.

    One run's error stops no other.
    """
    resumed = []
    for listed in await asyncio.to_thread(engine.store.list_runs, include_finished=False):
        try:
            run = engine.store.get_run(listed.run_id)
            standing = "ended" if run.job_id is None else job_standing(run.job_id)
            # A run this primary reserved or took names no attempt while its own call or job admission works on it.
            own = run.script_run_id is None and run.created_at >= engine.started_at
            own = own and (run.job_id is None or standing == "absent")
            if standing == "stopped":
                # Only while the run is still that job's: a resume may have given it to another one.
                await engine.cancel(run.run_id, reason=_JOB_STOPPED, job_id=run.job_id)
            elif standing == "absent" and run.attempt == 0 and not own:
                await asyncio.to_thread(engine.store.delete_intent, run.run_id)
            elif (run := await engine.settle(run, replay=True)).state is WorkflowRunState.STARTING:
                launched = standing in ("ended", "absent") and not own and await engine.launch(run.run_id)
                if launched and launched.attempt > run.attempt:
                    resumed.append(run.run_id)
        except WorkflowRunNotFoundError:
            continue
        except Exception:
            logger.exception("workflow_run_recovery_failed", run_id=listed.run_id)
    return resumed
