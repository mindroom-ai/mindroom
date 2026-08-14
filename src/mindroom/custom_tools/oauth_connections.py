"""Narrow agent-facing OAuth connection management tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.mcp.oauth import disconnect_mcp_oauth_request_session
from mindroom.oauth.registry import load_oauth_providers
from mindroom.oauth.service import (
    oauth_connect_url,
    oauth_credentials_worker_target,
    reset_scoped_oauth_credentials,
)
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

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

    async def reset_oauth_connection(self, provider_id: str) -> str:  # noqa: PLR0911
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

        tool_metadata = resolved_tool_metadata_for_runtime(
            self.runtime_paths,
            config,
            tolerate_plugin_load_errors=True,
        )
        allowed_provider_ids = {
            metadata.auth_provider
            for tool_name in config.resolve_entity(agent_name).available_tools
            if (metadata := tool_metadata.get(tool_name)) is not None and metadata.auth_provider is not None
        }
        if provider_id not in allowed_provider_ids:
            available = ", ".join(sorted(allowed_provider_ids)) or "none"
            return f"Error: Provider {provider_id!r} is not available to this agent. Available providers: {available}."

        provider = load_oauth_providers(config, self.runtime_paths).get(provider_id)
        if provider is None:
            return f"Error: OAuth provider {provider_id!r} is not configured."
        worker_target = oauth_credentials_worker_target(provider, self.worker_target)
        if worker_target is None or worker_target.worker_scope not in {"user", "user_agent"}:
            return "Error: Agent-initiated OAuth reset requires a requester-isolated user or user_agent scope."

        credentials_manager = get_runtime_credentials_manager(self.runtime_paths)
        deleted = await reset_scoped_oauth_credentials(
            provider.credential_service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        await disconnect_mcp_oauth_request_session(
            config.mcp_servers,
            provider.id,
            worker_target=worker_target,
        )
        connect_url = oauth_connect_url(provider, self.runtime_paths, worker_target=worker_target)
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
            f"`connect_url`: {connect_url}; reconnect it, then retry the request."
        )
