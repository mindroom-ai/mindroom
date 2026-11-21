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


NO_ACCESS = "The requester no longer has access."
_JOB_STOPPED = "The run's job was stopped before its next attempt started."


async def recover_runs(
    engine: WorkflowEngine,
    job_standing: Callable[[str], str],
    admission: Callable[[str], asyncio.Lock],
) -> list[str]:
    """Settle the runs whose attempts ended and return the runs that got their next attempt here.

    Called after the script runtime recorded which attempts ended. An ended attempt is settled whatever else holds:
    its run ends as the attempt left it, or waits for its next attempt when an interruption stopped it. Who starts
    that attempt follows from the run's owner: what the job runtime knows of its job at this moment
    (`workflow_job_standing`), or whether the call that waits for a run with no job is still here.

    - While the job is unfinished, only its operation does (`engine.wait`), which a Stop of the job cancels. The
      same holds while the job runtime cannot be asked, and for a call of this primary that waits for its run.
    - A job that was stopped ends its run. A reservation whose job the runtime does not hold was never admitted and
      is deleted, as that owner's and under the lock *admission* gives for the job. A delivery of the job's call
      holds that lock while the job is admitted, and then the reservation stays.
    - Any other run gets it here, under its saved owner as `engine.admit` allows, and only while it is still the
      owner's at the epoch this pass judged.

    One run's error stops no other.
    """
    resumed = []
    for listed in await asyncio.to_thread(engine.store.list_runs, include_finished=False):
        try:
            run = engine.store.get_run(listed.run_id)
            standing = "ended" if run.job_id is None else job_standing(run.job_id)
            if standing == "stopped":
                # Only while the run is still that job's: a resume may have given it to another one.
                await engine.cancel(run.run_id, owner=run.owner, **engine.job_stop(run, _JOB_STOPPED))
            elif standing == "absent" and run.attempt == 0:
                # Taken with no wait since the job was asked about, so no delivery of its call is in between.
                if not (lock := admission(str(run.job_id))).locked():
                    async with lock:
                        await asyncio.to_thread(engine.store.delete_intent, run.run_id, run.owner, run.epoch)
            elif (settled := await engine.settle(run, replay=True)).state is WorkflowRunState.STARTING:
                # What was asked about is the owner the run had: a run that another one took since is left to it.
                mine = standing in ("ended", "absent") and settled.owner == run.owner and not engine.attended(run.owner)
                launched = mine and await engine.launch(run.run_id, owner=run.owner, epoch=settled.epoch)
                if launched and launched.attempt > settled.attempt:
                    resumed.append(run.run_id)
        except WorkflowRunNotFoundError:
            continue
        except Exception:
            logger.exception("workflow_run_recovery_failed", run_id=listed.run_id)
    return resumed
