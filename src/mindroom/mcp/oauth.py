"""OAuth provider helpers for worker-scoped remote MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.oauth.discovery import (
    OAuthDiscoveryConfig,
    oauth_runtime_bootstrapper,
)
from mindroom.oauth.providers import OAuthProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.mcp.config import MCPOAuthConfig, MCPServerConfig
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def mcp_oauth_provider_id(server_id: str, auth_config: MCPOAuthConfig | None) -> str:
    """Return the OAuth provider id for one MCP server."""
    if auth_config is not None and auth_config.provider_id:
        return auth_config.provider_id
    return f"mcp_{server_id}"


def _mcp_oauth_credential_service(provider_id: str) -> str:
    """Return the token credential service for one generated MCP OAuth provider."""
    return f"{_mcp_oauth_service_prefix(provider_id)}_oauth"


def _mcp_oauth_client_config_service(provider_id: str) -> str:
    """Return the client registration credential service for one generated MCP OAuth provider."""
    return f"{_mcp_oauth_service_prefix(provider_id)}_oauth_client"


def _mcp_oauth_service_prefix(provider_id: str) -> str:
    """Return the credential-service prefix for one generated MCP OAuth provider."""
    return provider_id if provider_id.startswith("mcp_") else f"mcp_{provider_id}"


def _mcp_oauth_server_id_for_provider_id(
    mcp_servers: dict[str, MCPServerConfig],
    provider_id: str,
) -> str | None:
    """Return the MCP server id that generated one OAuth provider id."""
    for server_id, server_config in mcp_servers.items():
        if server_config.auth is None:
            continue
        if mcp_oauth_provider_id(server_id, server_config.auth) == provider_id:
            return server_id
    return None


async def disconnect_mcp_oauth_request_session(
    mcp_servers: dict[str, MCPServerConfig],
    provider_id: str,
    *,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    """Close the active worker-scoped MCP OAuth session for one generated provider."""
    server_id = _mcp_oauth_server_id_for_provider_id(mcp_servers, provider_id)
    if server_id is None:
        return
    manager = require_mcp_server_manager()
    if manager is not None:
        await manager.disconnect_request_session(server_id, worker_target=worker_target)


def _display_name(server_id: str, auth_config: MCPOAuthConfig) -> str:
    return auth_config.display_name or f"MCP {server_id.replace('_', ' ').title()}"


def _manual_endpoint(value: str | None, *, field_name: str, server_id: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    msg = f"MCP OAuth server '{server_id}' requires {field_name} until OAuth metadata discovery is configured"
    raise ValueError(msg)


def _configured_endpoint(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _oauth_discovery_config(server_config: MCPServerConfig) -> OAuthDiscoveryConfig:
    auth_config = server_config.auth
    if auth_config is None:
        msg = "MCP server is not OAuth-backed"
        raise ValueError(msg)
    return OAuthDiscoveryConfig(
        resource=auth_config.resource or server_config.url or "",
        discovery=auth_config.discovery,
        authorization_server=auth_config.authorization_server,
        authorization_url=auth_config.authorization_url,
        token_url=auth_config.token_url,
        registration_url=auth_config.registration_url,
        dynamic_client_registration=auth_config.dynamic_client_registration,
        token_endpoint_auth_method=auth_config.token_endpoint_auth_method,
        pkce_code_challenge_method=auth_config.pkce_code_challenge_method,
        allow_insecure_env="MINDROOM_MCP_OAUTH_ALLOW_INSECURE_DISCOVERY",
        allow_private_env="MINDROOM_MCP_OAUTH_ALLOW_PRIVATE_DISCOVERY",
        error_label="MCP OAuth",
    )


def mcp_oauth_provider(server_id: str, server_config: MCPServerConfig) -> OAuthProvider:
    """Build the generated OAuth provider for one OAuth-backed MCP server."""
    auth_config = server_config.auth
    if auth_config is None:
        msg = f"MCP server '{server_id}' is not OAuth-backed"
        raise ValueError(msg)

    provider_id = mcp_oauth_provider_id(server_id, auth_config)
    client_config_services = tuple(auth_config.client_config_services) or (
        _mcp_oauth_client_config_service(provider_id),
    )
    if auth_config.discovery == "manual":
        authorization_url = _manual_endpoint(
            auth_config.authorization_url,
            field_name="authorization_url",
            server_id=server_id,
        )
        token_url = _manual_endpoint(auth_config.token_url, field_name="token_url", server_id=server_id)
    else:
        authorization_url = _configured_endpoint(auth_config.authorization_url)
        token_url = _configured_endpoint(auth_config.token_url)
    return OAuthProvider(
        id=provider_id,
        display_name=_display_name(server_id, auth_config),
        authorization_url=authorization_url,
        token_url=token_url,
        scopes=tuple(auth_config.scopes),
        credential_service=_mcp_oauth_credential_service(provider_id),
        tool_config_service=None,
        client_config_services=client_config_services,
        shared_client_config_services=tuple(auth_config.shared_client_config_services),
        extra_auth_params=dict(auth_config.extra_auth_params),
        extra_token_params=dict(auth_config.extra_token_params),
        token_endpoint_auth_method=auth_config.token_endpoint_auth_method,
        pkce_code_challenge_method=auth_config.pkce_code_challenge_method,
        allow_empty_scopes=True,
        status_capabilities=(f"{_display_name(server_id, auth_config)} MCP access",),
        runtime_bootstrapper=oauth_runtime_bootstrapper(_oauth_discovery_config(server_config)),
    )


def mcp_oauth_providers_for_config(mcp_servers: dict[str, MCPServerConfig]) -> Iterable[OAuthProvider]:
    """Yield generated OAuth providers for OAuth-backed MCP servers."""
    for server_id, server_config in mcp_servers.items():
        if server_config.enabled and server_config.auth is not None:
            yield mcp_oauth_provider(server_id, server_config)
