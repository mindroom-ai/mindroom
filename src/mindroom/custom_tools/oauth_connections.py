"""Narrow agent-facing OAuth connection management tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.oauth.reset import OAuthResetTargetError
from mindroom.oauth.reset_execution import execute_oauth_connection_reset
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class OAuthConnectionTools(Toolkit):
    """Reset only the current requester's OAuth connections for the current agent."""

    def __init__(self, runtime_paths: RuntimePaths, *, worker_target: ResolvedWorkerTarget | None) -> None:
        self.runtime_paths = runtime_paths
        self.worker_target = worker_target
        super().__init__(
            name="oauth_connections",
            tools=[self.reset_oauth_connection],
            requires_confirmation_tools=["reset_oauth_connection"],
            stop_after_tool_call_tools=["reset_oauth_connection"],
        )

    async def reset_oauth_connection(self, provider_id: str) -> str:
        """Reset this agent's requester-scoped OAuth connection and return a fresh connect link.

        Use this only when an OAuth connection is stuck or revoked. The operation
        deletes the current requester's local credential in its resolved scope;
        user scope can affect this requester across agents. It does not revoke
        the grant at the provider. Human approval is always required.

        Args:
            provider_id: OAuth provider ID backing one of this agent's configured tools.

        Returns:
            An idempotent reset receipt with a requester-bound reconnect link.

        """
        runtime_context = get_tool_runtime_context()
        if runtime_context is None:
            return "Error: OAuth reset requires a live agent request context."
        config = runtime_context.config
        agent_name = self.worker_target.routing_agent_name if self.worker_target is not None else None
        if agent_name not in config.agents:
            return "Error: OAuth reset is available only during an agent request."
        if not is_sender_allowed_for_agent_credential_management(
            runtime_context.requester_id,
            agent_name=agent_name,
            config=config,
        ):
            return "Error: The current requester is not authorized to manage this agent's credentials."

        try:
            return await execute_oauth_connection_reset(
                provider_id,
                agent_name=agent_name,
                config=config,
                runtime_paths=self.runtime_paths,
                execution_identity=build_execution_identity_from_runtime_context(runtime_context),
                worker_target=self.worker_target,
                operation_id=(
                    runtime_context.approval_operation.operation_id
                    if runtime_context.approval_operation is not None
                    else None
                ),
            )
        except OAuthResetTargetError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: OAuth connection reset did not complete; verify connection status, then retry."
