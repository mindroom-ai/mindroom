"""Narrow agent-facing OAuth connection management tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.oauth.reset import OAuthResetTargetError, issue_browser_oauth_reset_url, resolve_oauth_reset_target
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class OAuthConnectionTools(Toolkit):
    """Issue browser-confirmed resets for OAuth connections the requester may manage."""

    def __init__(self, runtime_paths: RuntimePaths, *, worker_target: ResolvedWorkerTarget | None) -> None:
        self._runtime_paths = runtime_paths
        self._worker_target = worker_target
        super().__init__(
            name="oauth_connections",
            tools=[self.reset_oauth_connection],
        )

    async def reset_oauth_connection(self, provider_id: str) -> str:
        """Return a browser link to reset and reconnect an OAuth connection this requester may manage.

        Use this only when an OAuth connection is stuck or revoked. The operation
        opens a browser confirmation before changing credentials, and confirming it
        requires signing in to the MindRoom dashboard. User scope can affect this
        requester across agents; shared scope affects every requester of this agent.
        It does not revoke the grant at the provider.

        Args:
            provider_id: OAuth provider ID backing one of this agent's configured tools.

        Returns:
            A requester-issued browser reset link.

        """
        runtime_context = get_tool_runtime_context()
        if runtime_context is None:
            return "Error: OAuth reset requires a live agent request context."
        config = runtime_context.config
        agent_name = self._worker_target.routing_agent_name if self._worker_target is not None else None
        try:
            target = resolve_oauth_reset_target(
                provider_id,
                agent_name=agent_name,
                config=config,
                runtime_paths=self._runtime_paths,
                execution_identity=build_execution_identity_from_runtime_context(runtime_context),
                worker_target=self._worker_target,
            )
            reset_url = await issue_browser_oauth_reset_url(target)
        except OAuthResetTargetError as exc:
            return f"Error: {exc}"
        shared_guidance = (
            " Confirming it resets the connection for every requester of this agent."
            if target.worker_target.worker_scope == "shared"
            else ""
        )
        return (
            f"Open this requester-issued browser link to confirm resetting provider `{provider_id}`. "
            "Confirming it requires signing in to the MindRoom dashboard as a user who may manage this "
            "agent's connections, and no credentials change until you confirm in the browser. "
            f"`reset_url`: {reset_url}; the link is valid for 10 minutes.{shared_guidance}"
        )
