"""Single execution owner for scoped OAuth connection reset."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.mcp.errors import MCPError
from mindroom.mcp.oauth import retire_mcp_oauth_scope_session
from mindroom.oauth.credential_lifecycle import oauth_reset_operation_result, reset_oauth_credentials
from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.mcp.config import MCPServerConfig
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext


class OAuthResetPreparationError(RuntimeError):
    """Signal a reset failure before any durable reset intent could be published."""


async def retire_and_reset_oauth_credentials(
    context: OAuthCredentialContext,
    *,
    mcp_servers: Mapping[str, MCPServerConfig],
    operation_id: str | None,
    expected_connection_generation: str | None = None,
) -> bool:
    """Fence the exact MCP session and commit its credential reset once."""
    if operation_id is not None:
        try:
            completed = await oauth_reset_operation_result(context, operation_id)
        except OAuthProviderError as exc:
            msg = "OAuth connection reset preparation failed"
            raise OAuthResetPreparationError(msg) from exc
        if completed is not None:
            return completed
    reset_started = False
    try:
        async with retire_mcp_oauth_scope_session(
            dict(mcp_servers),
            context.provider.id,
            credential_context=context,
            expected_connection_generation=expected_connection_generation,
        ):
            reset_started = True
            return await reset_oauth_credentials(
                context,
                operation_id=operation_id,
                expected_connection_generation=expected_connection_generation,
            )
    except (MCPError, OAuthProviderError) as exc:
        if reset_started:
            raise
        msg = "OAuth connection reset preparation failed"
        raise OAuthResetPreparationError(msg) from exc
