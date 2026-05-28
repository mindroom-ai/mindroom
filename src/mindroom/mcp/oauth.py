"""OAuth provider helpers for requester-scoped remote MCP servers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import ParseResult, urlparse, urlunparse

import httpx

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.mcp.toolkit import require_mcp_server_manager
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError, OAuthRuntimeEndpoints

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPOAuthConfig, MCPServerConfig
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_DISCOVERY_TIMEOUT_SECONDS = 5.0
_DISCOVERY_CACHE_TTL_SECONDS = 3600.0
_JSON_CONTENT_TYPE = "application/json"
_DYNAMIC_CLIENT_SOURCE = "oauth_dynamic_client_registration"
_PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD = "none"  # noqa: S105
_TokenEndpointAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]


@dataclass(frozen=True, slots=True)
class _DiscoveredMCPOAuthMetadata:
    """Resolved OAuth metadata for one MCP protected resource."""

    authorization_url: str
    token_url: str
    registration_url: str | None
    token_endpoint_auth_method: _TokenEndpointAuthMethod


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    """Cached OAuth discovery result with an expiry."""

    metadata: _DiscoveredMCPOAuthMetadata
    expires_at: float


_DISCOVERY_CACHE: dict[tuple[object, ...], _CachedDiscovery] = {}


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
    """Close the active requester-scoped MCP OAuth session for one generated provider."""
    server_id = _mcp_oauth_server_id_for_provider_id(mcp_servers, provider_id)
    if server_id is None:
        return

    manager = require_mcp_server_manager()
    if manager is not None:
        await manager.disconnect_request_session(server_id, worker_target=worker_target)


def _display_name(server_id: str, auth_config: MCPOAuthConfig) -> str:
    if auth_config.display_name:
        return auth_config.display_name
    return f"MCP {server_id.replace('_', ' ').title()}"


def _manual_endpoint(value: str | None, *, field_name: str, server_id: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    msg = f"MCP OAuth server '{server_id}' requires {field_name} until OAuth metadata discovery is configured"
    raise ValueError(msg)


def _configured_endpoint(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _url_origin(parsed: ParseResult) -> str:
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _protected_resource_metadata_urls(resource: str) -> tuple[str, ...]:
    parsed = urlparse(resource)
    origin = _url_origin(parsed)
    base_url = f"{origin}/.well-known/oauth-protected-resource"
    path = parsed.path if parsed.path and parsed.path != "/" else ""
    urls = [base_url]
    if path:
        urls.append(f"{base_url}{path}")
    return tuple(dict.fromkeys(urls))


def _authorization_server_metadata_urls(authorization_server: str) -> tuple[str, ...]:
    parsed = urlparse(authorization_server)
    origin = _url_origin(parsed)
    path = parsed.path.rstrip("/")
    urls: list[str] = []
    if path:
        urls.append(f"{origin}/.well-known/oauth-authorization-server{path}")
    urls.append(f"{origin}/.well-known/oauth-authorization-server")
    if path:
        urls.append(f"{authorization_server.rstrip('/')}/.well-known/oauth-authorization-server")
    return tuple(dict.fromkeys(urls))


def _address_is_unsafe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolved_host_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return tuple(addresses)


async def _host_is_unsafe(hostname: str | None) -> bool:
    if not hostname:
        return True
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = await asyncio.to_thread(_resolved_host_addresses, hostname)
        return any(_address_is_unsafe(address) for address in addresses)
    return _address_is_unsafe(address)


async def _validate_discovery_url(url: str, runtime_paths: RuntimePaths) -> None:
    parsed = urlparse(url)
    allow_insecure = runtime_paths.env_flag("MINDROOM_MCP_OAUTH_ALLOW_INSECURE_DISCOVERY")
    allow_private = runtime_paths.env_flag("MINDROOM_MCP_OAUTH_ALLOW_PRIVATE_DISCOVERY")
    if parsed.scheme != "https" and not allow_insecure:
        msg = f"MCP OAuth discovery requires HTTPS URL: {url}"
        raise OAuthProviderError(msg)
    if await _host_is_unsafe(parsed.hostname) and not allow_private:
        msg = f"MCP OAuth discovery refused unsafe URL host: {parsed.hostname or ''}"
        raise OAuthProviderError(msg)


def _metadata_cache_key(server_id: str, server_config: MCPServerConfig) -> tuple[object, ...]:
    auth_config = server_config.auth
    return (
        server_id,
        server_config.url,
        json.dumps(auth_config.model_dump(mode="json"), sort_keys=True) if auth_config is not None else None,
    )


def _metadata_from_cache(server_id: str, server_config: MCPServerConfig) -> _DiscoveredMCPOAuthMetadata | None:
    cached = _DISCOVERY_CACHE.get(_metadata_cache_key(server_id, server_config))
    if cached is None or cached.expires_at <= time.time():
        return None
    return cached.metadata


def _store_metadata_cache(
    server_id: str,
    server_config: MCPServerConfig,
    metadata: _DiscoveredMCPOAuthMetadata,
) -> None:
    _DISCOVERY_CACHE[_metadata_cache_key(server_id, server_config)] = _CachedDiscovery(
        metadata=metadata,
        expires_at=time.time() + _DISCOVERY_CACHE_TTL_SECONDS,
    )


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    runtime_paths: RuntimePaths,
    *,
    optional: bool = False,
) -> dict[str, Any] | None:
    await _validate_discovery_url(url, runtime_paths)
    try:
        response = await client.get(url, headers={"Accept": _JSON_CONTENT_TYPE})
        if optional and getattr(response, "status_code", None) in {404, 410}:
            return None
        response.raise_for_status()
    except Exception as exc:
        if optional:
            return None
        msg = f"MCP OAuth metadata request failed for {url}"
        raise OAuthProviderError(msg) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        msg = f"MCP OAuth metadata at {url} is not a JSON object"
        raise OAuthProviderError(msg)
    return payload


async def _discover_protected_resource_authorization_server(
    client: httpx.AsyncClient,
    auth_config: MCPOAuthConfig,
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths,
) -> str:
    if auth_config.authorization_server:
        return auth_config.authorization_server.strip()
    resource = auth_config.resource or server_config.url
    if not resource:
        msg = "MCP OAuth discovery requires resource or remote server URL"
        raise OAuthProviderError(msg)
    for metadata_url in _protected_resource_metadata_urls(resource):
        metadata = await _fetch_json(client, metadata_url, runtime_paths, optional=True)
        if metadata is None:
            continue
        authorization_servers = metadata.get("authorization_servers")
        if isinstance(authorization_servers, list):
            for entry in authorization_servers:
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
    msg = "MCP OAuth protected-resource metadata did not advertise an authorization server"
    raise OAuthProviderError(msg)


async def _discover_authorization_server_metadata(
    client: httpx.AsyncClient,
    authorization_server: str,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    for metadata_url in _authorization_server_metadata_urls(authorization_server):
        metadata = await _fetch_json(client, metadata_url, runtime_paths, optional=True)
        if metadata is not None:
            return metadata
    msg = f"MCP OAuth authorization-server metadata was not found for {authorization_server}"
    raise OAuthProviderError(msg)


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_discovered_capabilities(
    auth_config: MCPOAuthConfig,
    metadata: dict[str, Any],
    *,
    token_endpoint_auth_method: str,
) -> None:
    supported_auth_methods = metadata.get("token_endpoint_auth_methods_supported")
    if isinstance(supported_auth_methods, list) and token_endpoint_auth_method not in supported_auth_methods:
        msg = (
            "MCP OAuth authorization server does not support configured "
            f"token_endpoint_auth_method '{token_endpoint_auth_method}'"
        )
        raise OAuthProviderError(msg)
    supported_pkce_methods = metadata.get("code_challenge_methods_supported")
    if (
        auth_config.pkce_code_challenge_method is not None
        and isinstance(supported_pkce_methods, list)
        and auth_config.pkce_code_challenge_method not in supported_pkce_methods
    ):
        msg = "MCP OAuth authorization server does not support configured PKCE challenge method"
        raise OAuthProviderError(msg)


async def _resolve_mcp_oauth_metadata(
    server_id: str,
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths,
) -> _DiscoveredMCPOAuthMetadata:
    cached = _metadata_from_cache(server_id, server_config)
    if cached is not None:
        return cached

    auth_config = server_config.auth
    if auth_config is None:
        msg = f"MCP server '{server_id}' is not OAuth-backed"
        raise OAuthProviderError(msg)

    if auth_config.discovery == "manual":
        metadata = _DiscoveredMCPOAuthMetadata(
            authorization_url=_manual_endpoint(
                auth_config.authorization_url,
                field_name="authorization_url",
                server_id=server_id,
            ),
            token_url=_manual_endpoint(auth_config.token_url, field_name="token_url", server_id=server_id),
            registration_url=_configured_endpoint(auth_config.registration_url) or None,
            token_endpoint_auth_method=auth_config.token_endpoint_auth_method,
        )
        _store_metadata_cache(server_id, server_config, metadata)
        return metadata

    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
        authorization_server = await _discover_protected_resource_authorization_server(
            client,
            auth_config,
            server_config,
            runtime_paths,
        )
        as_metadata = await _discover_authorization_server_metadata(client, authorization_server, runtime_paths)

    authorization_url = _configured_endpoint(auth_config.authorization_url) or _metadata_string(
        as_metadata,
        "authorization_endpoint",
    )
    token_url = _configured_endpoint(auth_config.token_url) or _metadata_string(as_metadata, "token_endpoint")
    if authorization_url is None or token_url is None:
        msg = "MCP OAuth authorization-server metadata did not include required endpoints"
        raise OAuthProviderError(msg)
    registration_url = _configured_endpoint(auth_config.registration_url) or _metadata_string(
        as_metadata,
        "registration_endpoint",
    )
    _validate_discovered_capabilities(
        auth_config,
        as_metadata,
        token_endpoint_auth_method=auth_config.token_endpoint_auth_method,
    )
    metadata = _DiscoveredMCPOAuthMetadata(
        authorization_url=authorization_url,
        token_url=token_url,
        registration_url=registration_url,
        token_endpoint_auth_method=auth_config.token_endpoint_auth_method,
    )
    _store_metadata_cache(server_id, server_config, metadata)
    return metadata


def _stored_client_config_exists(provider: OAuthProvider, runtime_paths: RuntimePaths) -> bool:
    return provider.client_config_resolution(runtime_paths) is not None


def _client_registration_payload(
    provider: OAuthProvider,
    auth_config: MCPOAuthConfig,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_name": provider.display_name,
        "redirect_uris": [provider.default_redirect_uri(runtime_paths)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_config.token_endpoint_auth_method,
    }
    if provider.scopes:
        payload["scope"] = " ".join(provider.scopes)
    return payload


async def _register_dynamic_client(
    provider: OAuthProvider,
    server_config: MCPServerConfig,
    metadata: _DiscoveredMCPOAuthMetadata,
    runtime_paths: RuntimePaths,
) -> None:
    auth_config = server_config.auth
    if auth_config is None or _stored_client_config_exists(provider, runtime_paths):
        return
    if not auth_config.dynamic_client_registration:
        return
    if metadata.registration_url is None:
        return

    await _validate_discovery_url(metadata.registration_url, runtime_paths)
    payload = _client_registration_payload(provider, auth_config, runtime_paths)
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(
                metadata.registration_url,
                json=payload,
                headers={"Accept": _JSON_CONTENT_TYPE, "Content-Type": _JSON_CONTENT_TYPE},
            )
            response.raise_for_status()
            registration = response.json()
    except Exception as exc:
        msg = "MCP OAuth dynamic client registration failed"
        raise OAuthProviderError(msg) from exc
    if not isinstance(registration, dict):
        msg = "MCP OAuth dynamic client registration response is not a JSON object"
        raise OAuthProviderError(msg)
    client_id = registration.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        msg = "MCP OAuth dynamic client registration did not return client_id"
        raise OAuthProviderError(msg)
    client_secret = registration.get("client_secret")
    stored_registration = _stored_client_registration(
        provider,
        runtime_paths,
        auth_config=auth_config,
        client_id=client_id,
        client_secret=client_secret,
        registration=registration,
    )
    service = provider.client_config_services[0]
    get_runtime_credentials_manager(runtime_paths).save_credentials(service, stored_registration)


def _stored_client_registration(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    auth_config: MCPOAuthConfig,
    client_id: str,
    client_secret: object,
    registration: dict[str, Any],
) -> dict[str, Any]:
    if auth_config.token_endpoint_auth_method != _PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD and (
        not isinstance(client_secret, str) or not client_secret.strip()
    ):
        msg = "MCP OAuth dynamic client registration did not return client_secret"
        raise OAuthProviderError(msg)

    stored_registration: dict[str, Any] = {
        "client_id": client_id.strip(),
        "redirect_uri": provider.default_redirect_uri(runtime_paths),
        "_source": _DYNAMIC_CLIENT_SOURCE,
        "_oauth_provider": provider.id,
    }
    if isinstance(client_secret, str) and client_secret.strip():
        stored_registration["client_secret"] = client_secret.strip()
    for key in (
        "client_id_issued_at",
        "client_secret_expires_at",
        "registration_client_uri",
        "registration_access_token",
        "token_endpoint_auth_method",
    ):
        value = registration.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            stored_registration[key] = value
    return stored_registration


def _mcp_runtime_bootstrapper(
    server_id: str,
    server_config: MCPServerConfig,
) -> Callable[[OAuthProvider, RuntimePaths], Awaitable[OAuthRuntimeEndpoints]]:
    async def bootstrap(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        metadata = await _resolve_mcp_oauth_metadata(server_id, server_config, runtime_paths)
        await _register_dynamic_client(provider, server_config, metadata, runtime_paths)
        return OAuthRuntimeEndpoints(
            authorization_url=metadata.authorization_url,
            token_url=metadata.token_url,
            token_endpoint_auth_method=metadata.token_endpoint_auth_method,
        )

    return bootstrap


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
        runtime_bootstrapper=_mcp_runtime_bootstrapper(server_id, server_config),
    )


def mcp_oauth_providers_for_config(mcp_servers: dict[str, MCPServerConfig]) -> Iterable[OAuthProvider]:
    """Yield generated OAuth providers for OAuth-backed MCP servers."""
    for server_id, server_config in mcp_servers.items():
        if server_config.enabled and server_config.auth is not None:
            yield mcp_oauth_provider(server_id, server_config)
