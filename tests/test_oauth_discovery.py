"""Tests for reusable protected-resource OAuth discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credential_policy import RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import OAuthDiscoveryConfig, OAuthProvider, oauth_runtime_bootstrapper
from mindroom.oauth.discovery import (
    _DISCOVERY_CACHE,
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS,
    _discover_metadata,
)
from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _CloudflareDiscoveryClient:
    gets: ClassVar[list[str]] = []
    posts: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def __aenter__(self) -> _CloudflareDiscoveryClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> _Response:
        del headers
        self.gets.append(url)
        if url == "https://superset.example.test/.well-known/oauth-protected-resource":
            return _Response({}, 404)
        if url == "https://superset.example.test/.well-known/oauth-authorization-server":
            return _Response(
                {
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        return _Response({}, 404)

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        del headers
        self.posts.append((url, json))
        return _Response({"client_id": "registered-public-client"}, 201)


@pytest.fixture(autouse=True)
def _allow_example_test_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    _DISCOVERY_CACHE.clear()
    _DYNAMIC_CLIENT_REGISTRATION_LOCKS.clear()
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )


@pytest.mark.asyncio
async def test_cloudflare_style_resource_metadata_registers_public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App-domain metadata should bootstrap a PKCE public client without a secret."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    _CloudflareDiscoveryClient.gets = []
    _CloudflareDiscoveryClient.posts = []
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _CloudflareDiscoveryClient)
    provider = OAuthProvider(
        id="superset",
        display_name="Superset",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="superset_oauth",
        client_config_services=("superset_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        extra_auth_params={"resource": "https://superset.example.test"},
        extra_token_params={"resource": "https://superset.example.test"},
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://superset.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )
    verifier = provider.issue_pkce_code_verifier()
    assert verifier is not None

    authorization_url = await provider.authorization_uri_async(
        runtime_paths,
        state="state-token",
        code_verifier=verifier,
    )

    query = parse_qs(urlparse(authorization_url).query)
    assert query["client_id"] == ["registered-public-client"]
    assert query["resource"] == ["https://superset.example.test"]
    assert query["code_challenge_method"] == ["S256"]
    assert _CloudflareDiscoveryClient.gets == [
        "https://superset.example.test/.well-known/oauth-protected-resource",
        "https://superset.example.test/.well-known/oauth-authorization-server",
    ]
    assert _CloudflareDiscoveryClient.posts == [
        (
            "https://auth.example.test/register",
            {
                "client_name": "Superset",
                "redirect_uris": ["https://mindroom.example.test/api/oauth/superset/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        ),
    ]
    assert get_runtime_credentials_manager(runtime_paths).load_credentials("superset_oauth_client") == {
        "client_id": "registered-public-client",
        "redirect_uri": "https://mindroom.example.test/api/oauth/superset/callback",
        "_source": "oauth_dynamic_client_registration",
        "_oauth_provider": "superset",
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["  ", "superset.example.test"])
async def test_auto_discovery_requires_an_absolute_resource(tmp_path: Path, resource: str) -> None:
    """Auto discovery should fail clearly when no protected resource is configured."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    with pytest.raises(OAuthProviderError, match="auto discovery requires a protected-resource URL"):
        await _discover_metadata(OAuthDiscoveryConfig(resource=resource), runtime_paths)


@pytest.mark.asyncio
async def test_cached_endpoints_are_revalidated_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached endpoints must not bypass current network safety checks."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    blocked_auth_host = False

    def resolve(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        address = "10.0.0.5" if blocked_auth_host and host == "auth.example.test" else "93.184.216.34"
        return [(0, 0, 0, "", (address, 0))]

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", resolve)
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _CloudflareDiscoveryClient)
    config = OAuthDiscoveryConfig(
        resource="https://superset.example.test",
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
    )
    await _discover_metadata(config, runtime_paths)
    blocked_auth_host = True

    with pytest.raises(OAuthProviderError, match="refused unsafe URL"):
        await _discover_metadata(config, runtime_paths)


@pytest.mark.asyncio
async def test_dynamic_registration_supports_shared_only_client_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DCR should persist clients to a provider's shared service when needed."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )
    monkeypatch.setattr("mindroom.oauth.discovery.httpx.AsyncClient", _CloudflareDiscoveryClient)
    provider = OAuthProvider(
        id="shared_example",
        display_name="Shared Example",
        authorization_url="",
        token_url="",
        scopes=(),
        allow_empty_scopes=True,
        credential_service="shared_example_oauth",
        shared_client_config_services=("shared_example_oauth_client",),
        token_endpoint_auth_method="none",  # noqa: S106
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=oauth_runtime_bootstrapper(
            OAuthDiscoveryConfig(
                resource="https://superset.example.test",
                token_endpoint_auth_method="none",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
        ),
    )

    await provider.runtime_endpoints(runtime_paths)

    stored = get_runtime_credentials_manager(runtime_paths).load_credentials("shared_example_oauth_client")
    assert stored is not None
    assert stored["client_id"] == "registered-public-client"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery_config", "error"),
    [
        (
            OAuthDiscoveryConfig(
                resource="",
                discovery="manual",
                authorization_url="https://auth.example.test/authorize",
                token_url="https://auth.example.test/token",  # noqa: S106
                token_endpoint_auth_method="none",  # noqa: S106
            ),
            "token endpoint auth method",
        ),
        (
            OAuthDiscoveryConfig(
                resource="",
                discovery="manual",
                authorization_url="https://auth.example.test/authorize",
                token_url="https://auth.example.test/token",  # noqa: S106
                pkce_code_challenge_method="S256",
            ),
            "PKCE method",
        ),
    ],
)
async def test_bootstrap_rejects_provider_method_mismatch(
    tmp_path: Path,
    discovery_config: OAuthDiscoveryConfig,
    error: str,
) -> None:
    """Discovery and provider runtime methods must agree."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    provider = OAuthProvider(
        id="mismatched",
        display_name="Mismatched",
        authorization_url="",
        token_url="",
        scopes=("read",),
        credential_service="mismatched_oauth",
        client_config_services=("mismatched_oauth_client",),
        runtime_bootstrapper=oauth_runtime_bootstrapper(discovery_config),
    )

    with pytest.raises(OAuthProviderError, match=error):
        await provider.runtime_endpoints(runtime_paths)
