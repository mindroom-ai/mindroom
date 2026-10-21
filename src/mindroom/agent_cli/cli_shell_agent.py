"""Standard-mode agents whose native shell commands can call the agent's other tools through `mindroom-agent`."""

from __future__ import annotations

import functools
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agno.agent import Agent

from mindroom.agent_cli.agno_compat_cli_checkpoint import cli_dispatch_active
from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.response_owner import bind_response_owner
from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.approval.tool_approval import approval_gated
from mindroom.custom_tools.job import is_job_function
from mindroom.logging_config import get_logger
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.agno_toolkits import AdaptedToolkit
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["STANDARD_CLI_NOTE", "CliShellAgent", "standard_cli_eligible", "wrap_native_shell_window"]

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

    ``owners`` maps each offered function to its authored toolkit; only the shell tool's own command is windowed.
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


def _cli_callable(function: Function) -> bool:
    """Offer only calls that finish inside the shell command, without approval, questions, or run control.

    Approval-gated functions were removed already where approvals cannot pause. The job controls stay with the reply:
    a job read through the CLI is never saved as the reply's own call, so it would be reported again.
    """
    return not (
        approval_gated(function)
        or is_job_function(function)
        or function.requires_user_input
        or function.external_execution
        or function.stop_after_tool_call
    )


class CliShellAgent(Agent):
    """Standard agent that binds the response's CLI owner to its own tools each attempt."""

    response_context: ResponseTurnContext | None = None
    output_file_policy: ToolOutputFilePolicy | None = None
    delegation_depth: int = 0
    refresh_scheduler: KnowledgeRefreshScheduler | None = None

    async def aget_tools(
        self,
        run_response: RunOutput,
        run_context: RunContext,
        session: AgentSession,
        user_id: str | None = None,
        check_mcp_tools: bool = True,
    ) -> list[Any]:
        """Return the native tools unchanged and offer the safe ones to `mindroom-agent`."""
        tools = await super().aget_tools(
            run_response,
            run_context,
            session,
            user_id=user_id,
            check_mcp_tools=check_mcp_tools,
        )
        lifetime = current_cli_lifetime()
        runtime = get_tool_runtime_context()
        if lifetime is None or runtime is None or runtime.orchestrator is None or self.response_context is None:
            return tools
        catalog = PreparedAgentToolCatalog(self, run_context, run_response, session, runtime)
        try:
            await catalog.prepare(tools, include=_cli_callable)
            owner = lifetime.owner
            if (
                owner is not None
                and owner.catalog.agent is self
                and owner.catalog.run_context.run_id == run_context.run_id
            ):
                # Agno rebuilds the tools with a fresh run context when it continues this paused run, such as
                # after a subagent returns, so the paused run's catalog retires and CLI calls join the continuation.
                await owner.retire_binding()
            bind_response_owner(
                catalog,
                runtime=runtime,
                lifetime=lifetime,
                response_context=self.response_context,
                run_id=run_context.run_id,
                context_documents={},
                output_file_policy=self.output_file_policy,
                delegation_depth=self.delegation_depth,
                refresh_scheduler=self.refresh_scheduler,
                failure_message=str,
            )
        except Exception:
            # The CLI is optional in standard mode; never fail the response for it.
            logger.exception("Standard-mode agent CLI is unavailable for this response", agent=self.id)
            await catalog.close()
        return tools
