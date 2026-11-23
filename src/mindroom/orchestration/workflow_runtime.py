"""Build the workflow engine on the script runtime and tie its life to the orchestrator's."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.constants import runtime_matrix_homeserver
from mindroom.custom_tools.workflow import (
    undelivered_workflow_jobs,
    workflow_job_admission,
    workflow_job_standing,
    workflow_limits,
)
from mindroom.logging_config import get_logger
from mindroom.script_runs.models import utc_timestamp
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.workflow_runs.agent_nodes import AgentRuntime
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.recovery import recover_runs
from mindroom.workflow_runs.store import WorkflowRunStore
from mindroom.workflow_runs.view import RunViews

if TYPE_CHECKING:
    import nio

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
    # What shows the runs in their rooms; None shows nothing.
    views: RunViews | None = None

    async def start(self) -> None:
        """Follow each pass of the script runtime, which records the attempts that ended, and show the runs."""
        self.engine.owner_access = self._access
        self.engine.agents = self._agents
        self.engine.served_by = self._served_by
        self.script_runtime.after_reconcile = self._maintain
        if self.views is not None:
            self.views.start()

    async def _maintain(self) -> None:
        """Settle the runs whose attempts ended, then delete the runs that ended before the script runtime's retention.

        A run that a restart, a reload or a lost worker interrupted gets its next attempt here: the first pass after
        a start comes once the script runtime retired the attempts it inherited and the job runtime holds its jobs.
        The script runtime prunes its own runs after this, inside the same pass, so this gets half of the pass's time,
        and an error or a timeout here is logged and waits for the next pass.
        """
        try:
            async with asyncio.timeout(self.script_runtime.pass_timeout_seconds / 2):
                paths = self.script_runtime.runtime_paths
                standing, admission = partial(workflow_job_standing, paths), partial(workflow_job_admission, paths)
                await recover_runs(self.engine, standing, admission)
                cutoff = datetime.now(UTC) - timedelta(seconds=self.script_runtime.retention_seconds)
                # A report that a job has not saved as its own end is kept, also while nobody can say which those are.
                undelivered = undelivered_workflow_jobs(paths)
                await asyncio.to_thread(self.engine.store.prune, utc_timestamp(cutoff), undelivered)
        except Exception:
            logger.exception("workflow_maintenance_pass_failed")

    def _agents(self) -> AgentRuntime | None:
        """Return the job runtime agent turns run on with the live configuration, or None while either is missing."""
        paths, config = self.script_runtime.runtime_paths, self.script_runtime.config_provider()
        jobs = get_background_runtime(paths)
        return None if jobs is None or config is None else AgentRuntime(jobs, config, paths)

    def _served_by(self, run: WorkflowRun) -> str | None:
        """Return the name of the running bot that serves a run: the team's for a run of its member, as it posts."""
        bot = self.script_runtime.resolver.serving_bot(cast("ScriptRunRecord", run))
        return None if bot is None else bot.agent_name

    def _access(self, run: WorkflowRun) -> ResumeAccess:
        """Return what the next attempt of a run starts with, from its saved owner's live runtime.

        The run saves its owner as its attempts do, so the script runtime's resolver answers for it, also before a
        first attempt: access that cannot be checked yet, while the owner's bot is not running, has not finished its
        first sync or room membership is still loading, is pending, and access that is gone is revoked. An agent
        that the live configuration no longer has, or no longer gives the `workflow` toolkit, is revoked whatever
        its bot does, and so is a run in a room its synced bot is no longer in.
        """
        resolver, owner = self.script_runtime.resolver, cast("ScriptRunRecord", run)
        config = self.script_runtime.config_provider()
        if config is not None and (
            run.agent_name not in config.agents
            or "workflow" not in config.resolve_agent(run.agent_name).tools.tool_names
        ):
            return "revoked"
        try:
            # The same answer every call of the run's attempts gets: the bot that serves the run in its room, and the
            # requester's access to the run's agent.
            authorized = resolver.is_authorized(owner, require_resolved_membership=True)
            if authorized is None:
                return "pending"
            if authorized:
                context = resolver.resolve(owner, correlation_id=f"workflow:{run.run_id}")
                return context, workflow_limits(context, run.agent_name)
        except ValueError:
            # The saved identity names another owner than the run's: nothing confirms the access.
            logger.warning("workflow_resume_owner_unresolved", run_id=run.run_id, exc_info=True)
        except RuntimeError:
            return "pending"
        return "revoked"

    async def shutdown(self) -> None:
        """Stop following the script runtime and stop the engine's tasks; saved handles keep what was in flight."""
        self.script_runtime.after_reconcile = None
        if self.views is not None:
            await self.views.close()
        await self.engine.close()


def build_workflow_runtime(script_runtime: ScriptRuntimeLifecycle) -> WorkflowRuntimeLifecycle:
    """Construct the workflow store, beside the script store, and the engine on the script runtime's manager and broker."""
    control_root = script_runtime.store.database_path.parents[1]
    store = WorkflowRunStore(control_root / "workflow_runs" / "workflow_runs.sqlite3")
    engine = WorkflowEngine(store, script_runtime.manager, script_runtime.broker)

    def bot(run: WorkflowRun) -> tuple[str, nio.AsyncClient] | None:
        """Return the account and the client of the running bot that serves the run, which posts what the run shows."""
        serving = script_runtime.resolver.serving_bot(cast("ScriptRunRecord", run))
        return None if serving is None or serving.client is None else (serving.agent_name, serving.client)

    def client(account: str) -> nio.AsyncClient | None:
        """Return the client the bot of *account* has now, as the account's sub-agent frames are sent with."""
        bot = script_runtime.resolver.bot_provider(account)
        return None if bot is None else bot.client

    def live_frames() -> bool:
        config = script_runtime.config_provider()
        return config is not None and config.defaults.streaming.live_frames

    homeserver_url = runtime_matrix_homeserver(script_runtime.runtime_paths)
    views = RunViews(engine, bot=bot, client=client, homeserver_url=homeserver_url, live_frames=live_frames)
    return WorkflowRuntimeLifecycle(engine=engine, script_runtime=script_runtime, views=views)
