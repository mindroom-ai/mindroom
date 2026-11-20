"""Build the workflow engine on the script runtime and tie its life to the orchestrator's."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.script_runs.models import utc_timestamp
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.store import WorkflowRunStore

if TYPE_CHECKING:
    from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle


logger = get_logger(__name__)


@dataclass(slots=True)
class WorkflowRuntimeLifecycle:
    """The one process-local workflow engine, and when it follows the script runtime's passes."""

    engine: WorkflowEngine
    script_runtime: ScriptRuntimeLifecycle

    async def start(self) -> None:
        """Follow each pass of the script runtime, which records the attempts that ended."""
        self.script_runtime.after_reconcile = self._maintain

    async def _maintain(self) -> None:
        """End the runs whose attempts ended, then delete the runs that ended before the script runtime's retention.

        The script runtime prunes its own runs after this, so an error here is logged and waits for the next pass.
        """
        try:
            await self.engine.reconcile()
            cutoff = datetime.now(UTC) - timedelta(seconds=self.script_runtime.retention_seconds)
            await asyncio.to_thread(self.engine.store.prune, utc_timestamp(cutoff))
        except Exception:
            logger.exception("workflow_maintenance_pass_failed")

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
