"""Reusable OAuth metadata discovery and dynamic client registration."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import ParseResult, urlparse, urlunparse

import httpx

from mindroom.credential_policy import (
    OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE,
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
)
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.providers import OAuthProvider, OAuthProviderError, OAuthRuntimeEndpoints
from mindroom.server_fetch_url import (
    ServerFetchAsyncHTTPTransport,
    ServerFetchUrlError,
    validate_server_fetch_url,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.constants import RuntimePaths

_DISCOVERY_TIMEOUT_SECONDS = 5.0
_DISCOVERY_CACHE_TTL_SECONDS = 3600.0
_CROSS_LOOP_LOCK_RETRY_SECONDS = 0.01
_JSON_CONTENT_TYPE = "application/json"
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
    token_endpoint_auth_method: _TokenEndpointAuthMethod = "client_secret_post"  # noqa: S105
    pkce_code_challenge_method: Literal["S256"] | None = None
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
_DYNAMIC_CLIENT_REGISTRATION_LOCKS: dict[str, threading.Lock] = {}
_DYNAMIC_CLIENT_REGISTRATION_LOCKS_GUARD = threading.Lock()


class _MetadataCandidateError(OAuthProviderError):
    """One metadata candidate existed but could not be read."""


@asynccontextmanager
async def _cross_loop_lock(lock: threading.Lock) -> AsyncIterator[None]:
    """Acquire one loop-neutral lock without occupying an executor worker while waiting."""
    # A threading.Lock has no cross-loop notification primitive, so bounded polling
    # is the only wait that neither binds to one loop nor consumes an executor worker.
    while not lock.acquire(blocking=False):  # noqa: ASYNC110
        await asyncio.sleep(_CROSS_LOOP_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        lock.release()


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


def _http_client(config: OAuthDiscoveryConfig, runtime_paths: RuntimePaths) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_DISCOVERY_TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=ServerFetchAsyncHTTPTransport(
            allow_private_networks=runtime_paths.env_flag(config.allow_private_env),
        ),
    )


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
        msg = f"{config.error_label} metadata request failed for {url}"
        raise _MetadataCandidateError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{config.error_label} metadata at {url} is not a JSON object"
        raise _MetadataCandidateError(msg)
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
    last_error: _MetadataCandidateError | None = None
    found_metadata = False
    for metadata_url in _protected_resource_metadata_urls(config.resource.strip()):
        try:
            metadata = await _fetch_json(client, metadata_url, config, runtime_paths, optional=True)
        except _MetadataCandidateError as exc:
            last_error = exc
            continue
        if metadata is None:
            continue
        found_metadata = True
        authorization_servers = metadata.get("authorization_servers")
        if isinstance(authorization_servers, list):
            for entry in authorization_servers:
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
    if last_error is not None and not found_metadata:
        raise last_error
    return None


async def _authorization_metadata(
    client: httpx.AsyncClient,
    config: OAuthDiscoveryConfig,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    authorization_server = await _authorization_server(client, config, runtime_paths)
    metadata_base = authorization_server or _url_origin(urlparse(config.resource.strip()))
    last_error: _MetadataCandidateError | None = None
    for metadata_url in _authorization_server_metadata_urls(metadata_base):
        try:
            metadata = await _fetch_json(client, metadata_url, config, runtime_paths, optional=True)
        except _MetadataCandidateError as exc:
            last_error = exc
            continue
        if metadata is not None:
            return metadata
    if last_error is not None:
        raise last_error
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
    if config.discovery == "auto":
        resource = config.resource.strip()
        parsed_resource = urlparse(resource)
        if not resource or not parsed_resource.scheme or not parsed_resource.netloc:
            msg = f"{config.error_label} auto discovery requires a protected-resource URL"
            raise OAuthProviderError(msg)
        await _validate_url(resource, config, runtime_paths)

    key = _cache_key(config, runtime_paths)
    cached = _DISCOVERY_CACHE.get(key)
    if cached is not None and cached.expires_at > time.time():
        await _validate_metadata(cached.metadata, config, runtime_paths)
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
        async with _http_client(config, runtime_paths) as client:
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
        "_source": OAUTH_DYNAMIC_CLIENT_REGISTRATION_SOURCE,
        "_oauth_provider": provider.id,
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
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
    with _DYNAMIC_CLIENT_REGISTRATION_LOCKS_GUARD:
        lock = _DYNAMIC_CLIENT_REGISTRATION_LOCKS.setdefault(provider.id, threading.Lock())
    async with _cross_loop_lock(lock):
        if provider.client_config_resolution(runtime_paths) is not None:
            return
        if not provider.client_config_services:
            msg = f"{config.error_label} dynamic client registration requires a provider-specific client config service"
            raise OAuthProviderError(msg)
        credentials_manager = get_runtime_credentials_manager(runtime_paths)
        if credentials_manager.current_worker_key is not None:
            msg = f"{config.error_label} dynamic client registration must run in the primary runtime"
            raise OAuthProviderError(msg)
        await _validate_url(metadata.registration_url, config, runtime_paths)
        try:
            async with _http_client(config, runtime_paths) as client:
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
        credentials_manager.save_credentials(
            service,
            _stored_registration(provider, runtime_paths, registration),
        )


def oauth_runtime_bootstrapper(
    config: OAuthDiscoveryConfig,
) -> Callable[[OAuthProvider, RuntimePaths], Awaitable[OAuthRuntimeEndpoints]]:
    """Build a provider bootstrapper using OAuth discovery and optional DCR."""

    async def bootstrap(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        if config.token_endpoint_auth_method != provider.token_endpoint_auth_method:
            msg = f"{config.error_label} token endpoint auth method must match the OAuth provider"
            raise OAuthProviderError(msg)
        if config.pkce_code_challenge_method != provider.pkce_code_challenge_method:
            msg = f"{config.error_label} PKCE method must match the OAuth provider"
            raise OAuthProviderError(msg)
        metadata = await _discover_metadata(config, runtime_paths)
        await _register_client(provider, config, metadata, runtime_paths)
        return OAuthRuntimeEndpoints(
            authorization_url=metadata.authorization_url,
            token_url=metadata.token_url,
            token_endpoint_auth_method=metadata.token_endpoint_auth_method,
        )

    return bootstrap
