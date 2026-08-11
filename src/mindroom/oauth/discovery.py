"""Reusable OAuth metadata discovery and dynamic client registration."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import ParseResult, urlparse, urlunparse

import httpx

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError, OAuthRuntimeEndpoints
from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths

_DISCOVERY_TIMEOUT_SECONDS = 5.0
_DISCOVERY_CACHE_TTL_SECONDS = 3600.0
_JSON_CONTENT_TYPE = "application/json"
_DYNAMIC_CLIENT_SOURCE = "oauth_dynamic_client_registration"
_PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD = "none"  # noqa: S105
_TokenEndpointAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]


@dataclass(frozen=True, slots=True)
class OAuthDiscoveryConfig:
    """Configuration for one protected-resource OAuth authorization server."""

    resource: str
    discovery: Literal["auto", "manual"] = "auto"
    authorization_server: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    registration_url: str | None = None
    dynamic_client_registration: bool = True
    token_endpoint_auth_method: _TokenEndpointAuthMethod = "none"  # noqa: S105
    pkce_code_challenge_method: Literal["S256"] | None = "S256"
    allow_insecure_env: str = "MINDROOM_OAUTH_ALLOW_INSECURE_DISCOVERY"
    allow_private_env: str = "MINDROOM_OAUTH_ALLOW_PRIVATE_DISCOVERY"
    error_label: str = "OAuth"


@dataclass(frozen=True, slots=True)
class _DiscoveredOAuthMetadata:
    authorization_url: str
    token_url: str
    registration_url: str | None
    token_endpoint_auth_method: _TokenEndpointAuthMethod


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    metadata: _DiscoveredOAuthMetadata
    expires_at: float


_DISCOVERY_CACHE: dict[tuple[object, ...], _CachedDiscovery] = {}
_DYNAMIC_CLIENT_REGISTRATION_LOCKS: dict[str, asyncio.Lock] = {}


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


async def _validate_url(url: str, config: OAuthDiscoveryConfig, runtime_paths: RuntimePaths) -> None:
    parsed = urlparse(url)
    allow_insecure = runtime_paths.env_flag(config.allow_insecure_env)
    allow_private = runtime_paths.env_flag(config.allow_private_env)
    if parsed.scheme != "https" and not allow_insecure:
        msg = f"{config.error_label} discovery requires HTTPS URL: {url}"
        raise OAuthProviderError(msg)
    try:
        await asyncio.to_thread(validate_server_fetch_url, url, allow_private_networks=allow_private)
    except ServerFetchUrlError as exc:
        msg = f"{config.error_label} discovery refused unsafe URL"
        raise OAuthProviderError(msg) from exc


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
    *,
    optional: bool = False,
) -> dict[str, Any] | None:
    await _validate_url(url, config, runtime_paths)
    try:
        response = await client.get(url, headers={"Accept": _JSON_CONTENT_TYPE})
        if optional and response.status_code in {404, 410}:
            return None
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        if optional:
            return None
        msg = f"{config.error_label} metadata request failed for {url}"
        raise OAuthProviderError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{config.error_label} metadata at {url} is not a JSON object"
        raise OAuthProviderError(msg)
    return payload


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _authorization_server(
    client: httpx.AsyncClient,
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
) -> str | None:
    if config.authorization_server:
        return config.authorization_server.strip()
    for metadata_url in _protected_resource_metadata_urls(config.resource):
        metadata = await _fetch_json(client, metadata_url, config, runtime_paths, optional=True)
        if metadata is None:
            continue
        authorization_servers = metadata.get("authorization_servers")
        if isinstance(authorization_servers, list):
            for entry in authorization_servers:
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
    return None


async def _authorization_metadata(
    client: httpx.AsyncClient,
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    authorization_server = await _authorization_server(client, config, runtime_paths)
    metadata_base = authorization_server or _url_origin(urlparse(config.resource))
    for metadata_url in _authorization_server_metadata_urls(metadata_base):
        metadata = await _fetch_json(client, metadata_url, config, runtime_paths, optional=True)
        if metadata is not None:
            return metadata
    msg = f"{config.error_label} authorization-server metadata was not found for {metadata_base}"
    raise OAuthProviderError(msg)


def _validate_capabilities(config: OAuthDiscoveryConfig, metadata: dict[str, Any]) -> None:
    supported_auth_methods = metadata.get("token_endpoint_auth_methods_supported")
    if isinstance(supported_auth_methods, list) and config.token_endpoint_auth_method not in supported_auth_methods:
        msg = (
            f"{config.error_label} authorization server does not support configured "
            f"token_endpoint_auth_method '{config.token_endpoint_auth_method}'"
        )
        raise OAuthProviderError(msg)
    supported_pkce_methods = metadata.get("code_challenge_methods_supported")
    if (
        config.pkce_code_challenge_method is not None
        and isinstance(supported_pkce_methods, list)
        and config.pkce_code_challenge_method not in supported_pkce_methods
    ):
        msg = f"{config.error_label} authorization server does not support configured PKCE challenge method"
        raise OAuthProviderError(msg)


def _cache_key(config: OAuthDiscoveryConfig, runtime_paths: RuntimePaths) -> tuple[object, ...]:
    return (
        config,
        runtime_paths.env_flag(config.allow_insecure_env),
        runtime_paths.env_flag(config.allow_private_env),
    )


async def _validate_metadata(
    metadata: _DiscoveredOAuthMetadata,
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
) -> None:
    await _validate_url(metadata.authorization_url, config, runtime_paths)
    await _validate_url(metadata.token_url, config, runtime_paths)
    if metadata.registration_url is not None:
        await _validate_url(metadata.registration_url, config, runtime_paths)


async def _discover_metadata(
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
) -> _DiscoveredOAuthMetadata:
    key = _cache_key(config, runtime_paths)
    cached = _DISCOVERY_CACHE.get(key)
    if cached is not None and cached.expires_at > time.time():
        return cached.metadata

    if config.discovery == "manual":
        authorization_url = _configured_endpoint(config.authorization_url)
        token_url = _configured_endpoint(config.token_url)
        if not authorization_url or not token_url:
            msg = f"{config.error_label} manual discovery requires authorization_url and token_url"
            raise OAuthProviderError(msg)
        metadata = _DiscoveredOAuthMetadata(
            authorization_url=authorization_url,
            token_url=token_url,
            registration_url=_configured_endpoint(config.registration_url) or None,
            token_endpoint_auth_method=config.token_endpoint_auth_method,
        )
    else:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
            discovered = await _authorization_metadata(client, config, runtime_paths)
        _validate_capabilities(config, discovered)
        authorization_url = _configured_endpoint(config.authorization_url) or _metadata_string(
            discovered,
            "authorization_endpoint",
        )
        token_url = _configured_endpoint(config.token_url) or _metadata_string(discovered, "token_endpoint")
        if authorization_url is None or token_url is None:
            msg = f"{config.error_label} authorization-server metadata did not include required endpoints"
            raise OAuthProviderError(msg)
        metadata = _DiscoveredOAuthMetadata(
            authorization_url=authorization_url,
            token_url=token_url,
            registration_url=_configured_endpoint(config.registration_url)
            or _metadata_string(discovered, "registration_endpoint"),
            token_endpoint_auth_method=config.token_endpoint_auth_method,
        )

    await _validate_metadata(metadata, config, runtime_paths)
    _DISCOVERY_CACHE[key] = _CachedDiscovery(
        metadata=metadata,
        expires_at=time.time() + _DISCOVERY_CACHE_TTL_SECONDS,
    )
    return metadata


def _registration_payload(provider: OAuthProvider, runtime_paths: RuntimePaths) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_name": provider.display_name,
        "redirect_uris": [provider.default_redirect_uri(runtime_paths)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": provider.token_endpoint_auth_method,
    }
    if provider.scopes:
        payload["scope"] = " ".join(provider.scopes)
    return payload


def _stored_registration(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    registration: dict[str, Any],
) -> dict[str, Any]:
    client_id = registration.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        msg = f"{provider.display_name} OAuth dynamic client registration did not return client_id"
        raise OAuthProviderError(msg)
    client_secret = registration.get("client_secret")
    if provider.token_endpoint_auth_method != _PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD and (
        not isinstance(client_secret, str) or not client_secret.strip()
    ):
        msg = f"{provider.display_name} OAuth dynamic client registration did not return client_secret"
        raise OAuthProviderError(msg)
    stored: dict[str, Any] = {
        "client_id": client_id.strip(),
        "redirect_uri": provider.default_redirect_uri(runtime_paths),
        "_source": _DYNAMIC_CLIENT_SOURCE,
        "_oauth_provider": provider.id,
    }
    if isinstance(client_secret, str) and client_secret.strip():
        stored["client_secret"] = client_secret.strip()
    for key in (
        "client_id_issued_at",
        "client_secret_expires_at",
        "registration_client_uri",
        "registration_access_token",
        "token_endpoint_auth_method",
    ):
        value = registration.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            stored[key] = value
    return stored


async def _register_client(
    provider: OAuthProvider,
    config: OAuthDiscoveryConfig,
    metadata: _DiscoveredOAuthMetadata,
    runtime_paths: RuntimePaths,
) -> None:
    if not config.dynamic_client_registration or metadata.registration_url is None:
        return
    lock = _DYNAMIC_CLIENT_REGISTRATION_LOCKS.setdefault(provider.id, asyncio.Lock())
    async with lock:
        if provider.client_config_resolution(runtime_paths) is not None:
            return
        await _validate_url(metadata.registration_url, config, runtime_paths)
        try:
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(
                    metadata.registration_url,
                    json=_registration_payload(provider, runtime_paths),
                    headers={"Accept": _JSON_CONTENT_TYPE, "Content-Type": _JSON_CONTENT_TYPE},
                )
                response.raise_for_status()
                registration = response.json()
        except Exception as exc:
            msg = f"{config.error_label} dynamic client registration failed"
            raise OAuthProviderError(msg) from exc
        if not isinstance(registration, dict):
            msg = f"{config.error_label} dynamic client registration response is not a JSON object"
            raise OAuthProviderError(msg)
        service = provider.client_config_services[0]
        get_runtime_credentials_manager(runtime_paths).save_credentials(
            service,
            _stored_registration(provider, runtime_paths, registration),
        )


def oauth_runtime_bootstrapper(
    config: OAuthDiscoveryConfig,
) -> Callable[[OAuthProvider, RuntimePaths], Awaitable[OAuthRuntimeEndpoints]]:
    """Build a provider bootstrapper using OAuth discovery and optional DCR."""

    async def bootstrap(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        metadata = await _discover_metadata(config, runtime_paths)
        await _register_client(provider, config, metadata, runtime_paths)
        return OAuthRuntimeEndpoints(
            authorization_url=metadata.authorization_url,
            token_url=metadata.token_url,
            token_endpoint_auth_method=metadata.token_endpoint_auth_method,
        )

    return bootstrap
