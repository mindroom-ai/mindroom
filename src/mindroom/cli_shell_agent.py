"""Standard-mode agents whose native shell commands can call the agent's other tools through `mindroom-agent`."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.response_owner import bind_response_owner
from mindroom.agent_cli.shell_access import minimal_shell_problems
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.agno_compat_cli_checkpoint import cli_dispatch_active
from mindroom.logging_config import get_logger
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.declarations import declare_tool_schema_source
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.session import AgentSession
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.response_turn import ResponseTurnContext
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


def wrap_native_shell_window(tools: Sequence[Toolkit]) -> bool:
    """Run each native `run_shell_command` inside the response's CLI window; return whether one was found."""
    wrapped = False
    for toolkit in tools:
        function = toolkit.get_async_functions().get("run_shell_command")
        # An approved command resumes in a later run without this response's CLI, like a minimal subagent's.
        if (
            function is None
            or function.owning_toolkit != "shell"
            or function.entrypoint is None
            or function.requires_confirmation
        ):
            continue
        original = function.entrypoint

        @functools.wraps(original)
        async def windowed(*args: Any, _original: Any = original, **kwargs: Any) -> Any:  # noqa: ANN401
            lifetime = current_cli_lifetime()
            owner = lifetime.owner if lifetime is not None else None
            # Nested CLI shell calls already run inside their parent's window.
            if owner is None or owner.shell_env is None or cli_dispatch_active():
                return await _original(*args, **kwargs)
            return await owner.run_native_shell(functools.partial(_original, *args, **kwargs))

        declare_tool_schema_source(windowed, original)
        function.entrypoint = windowed
        wrapped = True
    return wrapped


def _cli_callable(function: Function) -> bool:
    """Offer only calls that finish inside the shell command, without approval, questions, or run control.

    Approval-gated functions carry `requires_confirmation`, or were removed where approvals cannot pause.
    """
    return not (
        function.requires_confirmation
        or function.requires_user_input
        or function.external_execution
        or function.stop_after_tool_call
    )


class CliShellAgent(KnowledgeToolDescribingAgent):
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
