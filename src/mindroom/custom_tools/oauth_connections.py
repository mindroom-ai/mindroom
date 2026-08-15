"""Narrow agent-facing OAuth connection management tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.mcp.oauth import disconnect_mcp_oauth_request_session
from mindroom.oauth.reset import OAuthResetTargetError, resolve_oauth_reset_target
from mindroom.oauth.service import (
    OAUTH_CONNECT_TOKEN_TTL_MINUTES,
    oauth_connect_url,
    reset_scoped_oauth_credentials,
)
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)


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
            reset_target = resolve_oauth_reset_target(
                provider_id,
                agent_name=agent_name,
                config=config,
                runtime_paths=self.runtime_paths,
                execution_identity=build_execution_identity_from_runtime_context(runtime_context),
                worker_target=self.worker_target,
            )
        except OAuthResetTargetError as exc:
            return f"Error: {exc}"
        provider = reset_target.provider
        worker_target = reset_target.worker_target

        connect_url = oauth_connect_url(provider, self.runtime_paths, worker_target=worker_target)
        credentials_manager = get_runtime_credentials_manager(self.runtime_paths)
        deleted = await reset_scoped_oauth_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        try:
            await disconnect_mcp_oauth_request_session(
                config.mcp_servers,
                provider.id,
                worker_target=worker_target,
            )
        except Exception as exc:
            logger.warning(
                "oauth_mcp_session_disconnect_failed",
                provider_id=provider.id,
                agent_name=agent_name,
                error_type=type(exc).__name__,
            )
        scope_receipt = (
            "for this requester across agents"
            if worker_target.worker_scope == "user"
            else "for this requester and agent"
        )
        logger.info(
            "oauth_connection_reset",
            provider_id=provider.id,
            agent_name=agent_name,
            credential_existed=deleted,
        )
        return (
            f"OAuth connection reset {scope_receipt} for provider `{provider.id}`. "
            f"`connect_url`: {connect_url}; reconnect it, then retry the request. "
            f"This link is valid for {OAUTH_CONNECT_TOKEN_TTL_MINUTES} minutes; "
            "if it expires, run this reset again for a fresh link."
        )
