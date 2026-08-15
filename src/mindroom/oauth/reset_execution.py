"""Single execution owner for requester-scoped OAuth connection reset."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.mcp.oauth import retire_mcp_oauth_request_session
from mindroom.oauth.credential_lifecycle import oauth_reset_operation_result, reset_oauth_credentials
from mindroom.oauth.reset import resolve_oauth_reset_target
from mindroom.oauth.service import OAUTH_CONNECT_TOKEN_TTL_MINUTES, oauth_connect_url

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

logger = get_logger(__name__)


async def retire_and_reset_oauth_credentials(
    context: OAuthCredentialContext,
    *,
    mcp_servers: Mapping[str, MCPServerConfig],
    operation_id: str | None,
    expected_connection_generation: str | None = None,
    before_reset: Callable[[], None] | None = None,
) -> bool:
    """Fence the exact MCP session and commit its credential reset once."""
    async with retire_mcp_oauth_request_session(
        dict(mcp_servers),
        context.provider.id,
        credential_context=context,
        expected_connection_generation=expected_connection_generation,
    ):
        if before_reset is not None:
            before_reset()
        return await reset_oauth_credentials(
            context,
            operation_id=operation_id,
            expected_connection_generation=expected_connection_generation,
        )


async def execute_oauth_connection_reset(
    provider_id: str,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    operation_id: str | None,
    expected_connection_generation: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> str:
    """Resolve, retire, durably reset, and render one idempotent receipt."""
    reset_target = resolve_oauth_reset_target(
        provider_id,
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        worker_target=worker_target,
    )
    provider = reset_target.provider
    resolved_worker_target = reset_target.worker_target
    connect_url: str | None = None

    def mint_connect_url() -> None:
        nonlocal connect_url
        connect_url = oauth_connect_url(provider, runtime_paths, worker_target=resolved_worker_target)

    try:
        completed_result = (
            oauth_reset_operation_result(reset_target.credential_context, operation_id)
            if operation_id is not None
            else None
        )
        if completed_result is None:
            deleted = await retire_and_reset_oauth_credentials(
                reset_target.credential_context,
                mcp_servers=config.mcp_servers,
                operation_id=operation_id,
                expected_connection_generation=expected_connection_generation,
                before_reset=mint_connect_url,
            )
        else:
            mint_connect_url()
            deleted = completed_result
    except Exception as exc:
        logger.warning(
            "oauth_connection_reset_failed",
            provider_id=provider.id,
            agent_name=agent_name,
            error_type=type(exc).__name__,
        )
        raise
    assert connect_url is not None
    scope_receipt = (
        "for this requester across agents"
        if resolved_worker_target.worker_scope == "user"
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
