"""Standard-mode agents whose native shell commands can call the agent's other tools through `mindroom-agent`."""

from __future__ import annotations

import functools
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.response_owner import bind_response_owner
from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.approval.tool_approval import approval_gated
from mindroom.custom_tools.job import is_job_function
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog, cli_dispatch_active
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.spec import RunContext
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["STANDARD_CLI_NOTE", "bind_standard_cli", "standard_cli_eligible", "wrap_native_shell_window"]

logger = get_logger(__name__)

STANDARD_CLI_NOTE = (
    "Inside shell commands, `mindroom-agent` calls your other tools by name, so one script can combine many calls; "
    "run `mindroom-agent --help` for usage."
)


def standard_cli_eligible(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    execution_identity: ToolExecutionIdentity | None,
) -> bool:
    """Return whether this standard agent's shell can reach MindRoom's CLI routes in a Matrix conversation."""
    return (
        execution_identity is not None
        and execution_identity.channel == "matrix"
        and not minimal_shell_problems(config, runtime_paths, agent_name)
    )


def wrap_native_shell_window(
    toolkits: Sequence[AdaptedToolkit],
    owners: Mapping[str, str],
) -> tuple[AdaptedToolkit, ...] | None:
    """Return ``toolkits`` with the native `run_shell_command` running inside its own CLI window; None without one.

    ``owners`` maps each configured function to its authored toolkit; only the shell tool's own command is windowed.
    """
    if owners.get("run_shell_command") != "shell":
        return None
    for index, toolkit in enumerate(toolkits):
        for position, function in enumerate(toolkit.functions):
            if function.name != "run_shell_command":
                continue
            # An approved command resumes in a later run without this response's CLI, like a minimal subagent's.
            if function.requires_confirmation:
                return None
            functions = list(toolkit.functions)
            functions[position] = replace(function, entrypoint=_windowed(function.entrypoint))
            return (*toolkits[:index], replace(toolkit, functions=tuple(functions)), *toolkits[index + 1 :])
    return None


def _windowed(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    async def windowed(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        lifetime = current_cli_lifetime()
        owner = lifetime.owner if lifetime is not None else None
        # Nested CLI shell calls already run inside their parent's window.
        if owner is None or owner.shell_env is None or cli_dispatch_active():
            return await original(*args, **kwargs)
        return await owner.run_native_shell(functools.partial(original, *args, **kwargs))

    return windowed


def _cli_callable(function: ToolFunction) -> bool:
    """Offer only calls that finish inside the shell command, without approval or run control.

    Approval-gated functions were removed already where approvals cannot pause. The job controls stay with the reply:
    a job read through the CLI is never saved as the reply's own call, so it would be reported again.
    """
    return not (
        approval_gated(function)
        or is_job_function(function)
        or function.external_execution
        or function.stop_after_tool_call
    )


async def bind_standard_cli(
    build: AgentBuild,
    *,
    run: RunContext,
    store: ConversationStore | None,
    response_context: ResponseTurnContext,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> None:
    """Offer the build's safe functions to `mindroom-agent` for this attempt; the run's own tools stay as they are."""
    lifetime = current_cli_lifetime()
    runtime = get_tool_runtime_context()
    if lifetime is None or runtime is None or runtime.orchestrator is None:
        return
    catalog = PreparedAgentToolCatalog(
        agent_name=build.spec.id,
        run=run,
        runtime_context=runtime,
        store=store,
        middleware=agent_tool_middleware(build),
        tool_call_limit=build.spec.tool_call_limit,
    )
    try:
        await catalog.prepare(build.spec.toolkits, build.tool_owners, include=_cli_callable)
        bind_response_owner(
            catalog,
            runtime=runtime,
            lifetime=lifetime,
            response_context=response_context,
            run_id=run.run_id,
            context_documents={},
            output_file_policy=build.surface.output_file_policy,
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
            failure_message=str,
        )
    except Exception:
        # The CLI is optional in standard mode; never fail the response for it.
        logger.exception("Standard-mode agent CLI is unavailable for this response", agent=build.spec.id)
        await catalog.close()
