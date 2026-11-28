"""Standard-mode agents whose native shell commands can call the agent's other tools through `mindroom-agent`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.response_owner import bind_response_owner, tool_catalog
from mindroom.custom_tools.job import is_job_function
from mindroom.delegation.state import is_delegation_function
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.spec import RunContext
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.run_storage import RunStorage
    from mindroom.tool_system.tool_function import ToolFunction

__all__ = ["bind_standard_cli"]

logger = get_logger(__name__)


def _cli_callable(function: ToolFunction) -> bool:
    """Offer only calls that finish inside the shell command, without a gate, run control, or child agent.

    Approval-gated functions were removed already where approvals cannot pause. The job controls stay with the reply:
    a job read through the CLI is never saved as the reply's own call, so it would be reported again. A subagent call
    runs its child as the reply's own call or job, never inside a shell command.
    """
    return not (
        function.gate is not None
        or is_job_function(function)
        or function.controls_run
        or is_delegation_function(function)
    )


async def bind_standard_cli(
    build: AgentBuild,
    *,
    run: RunContext,
    storage: RunStorage,
    response_context: ResponseTurnContext,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> None:
    """Offer the build's safe functions to `mindroom-agent` for this attempt; the run's own tools stay as they are."""
    lifetime = current_cli_lifetime()
    runtime = get_tool_runtime_context()
    if lifetime is None or runtime is None or runtime.orchestrator is None:
        return
    catalog = tool_catalog(build, run=run, runtime=runtime, storage=storage)
    try:
        await catalog.prepare(build.spec.toolkits, include=_cli_callable)
        bind_response_owner(
            catalog,
            lifetime=lifetime,
            response_context=response_context,
            context_documents={},
            output_file_policy=build.surface.output_file_policy,
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
            reload_ends_run=False,
        )
    except Exception:
        # The CLI is optional in standard mode; never fail the response for it.
        logger.exception("Standard-mode agent CLI is unavailable for this response", agent=build.spec.id)
        await catalog.close()
