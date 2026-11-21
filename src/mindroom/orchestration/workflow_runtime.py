"""Build the workflow engine on the script runtime and tie its life to the orchestrator's."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.custom_tools.workflow import workflow_job_standing, workflow_limits
from mindroom.logging_config import get_logger
from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.recovery import recover_runs
from mindroom.workflow_runs.store import WorkflowRunStore

if TYPE_CHECKING:
    from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.workflow_runs.engine import ResumeAccess
    from mindroom.workflow_runs.models import WorkflowRun


logger = get_logger(__name__)


@dataclass(slots=True)
class WorkflowRuntimeLifecycle:
    """The one process-local workflow engine, and when it follows the script runtime's passes."""

    engine: WorkflowEngine
    script_runtime: ScriptRuntimeLifecycle

    async def start(self) -> None:
        """Follow each pass of the script runtime, which records the attempts that ended."""
        self.engine.owner_access = self._access
        self.script_runtime.after_reconcile = self._maintain

    async def _maintain(self) -> None:
        """Settle the runs whose attempts ended, then delete the runs that ended before the script runtime's retention.

        A run that a restart, a reload or a lost worker interrupted gets its next attempt here: the first pass after
        a start comes once the script runtime retired the attempts it inherited and the job runtime holds its jobs.
        The script runtime prunes its own runs after this, inside the same pass, so this gets half of the pass's time,
        and an error or a timeout here is logged and waits for the next pass.
        """
        try:
            async with asyncio.timeout(self.script_runtime.pass_timeout_seconds / 2):
                standing = partial(workflow_job_standing, self.script_runtime.runtime_paths)
                await recover_runs(self.engine, self._access, standing)
                cutoff = datetime.now(UTC) - timedelta(seconds=self.script_runtime.retention_seconds)
                await asyncio.to_thread(self.engine.store.prune, utc_timestamp(cutoff))
        except Exception:
            logger.exception("workflow_maintenance_pass_failed")

    def _access(self, run: WorkflowRun) -> ResumeAccess:
        """Return what the next attempt of a run starts with, from its saved owner's live runtime.

        The run saves its owner as its attempts do, so the script runtime's resolver answers for it, also before a
        first attempt: access that cannot be checked yet, while the owner's bot is not running or room membership
        is still loading, is pending, and access that is gone is revoked.
        """
        resolver, owner = self.script_runtime.resolver, cast("ScriptRunRecord", run)
        try:
            authorized = resolver.is_authorized(owner, require_resolved_membership=True)
            if authorized is None:
                return "pending"
            if authorized:
                context = resolver.resolve(owner, correlation_id=f"workflow:{run.run_id}")
                return context, workflow_limits(context.config, run.agent_name)
        except ValueError:
            # The saved identity names another owner than the run's: nothing confirms the access.
            logger.warning("workflow_resume_owner_unresolved", run_id=run.run_id, exc_info=True)
        except RuntimeError:
            return "pending"
        return "revoked"

    async def shutdown(self) -> None:
        """Stop following the script runtime and stop the engine's tasks; saved handles keep what was in flight."""
        self.script_runtime.after_reconcile = None
        await self.engine.close()


def build_workflow_runtime(script_runtime: ScriptRuntimeLifecycle) -> WorkflowRuntimeLifecycle:
    """Construct the workflow store, beside the script store, and the engine on the script runtime's manager and broker."""
    control_root = script_runtime.store.database_path.parents[1]
    store = WorkflowRunStore(control_root / "workflow_runs" / "workflow_runs.sqlite3")
    engine = WorkflowEngine(store, script_runtime.manager, script_runtime.broker)
    return WorkflowRuntimeLifecycle(engine=engine, script_runtime=script_runtime)
