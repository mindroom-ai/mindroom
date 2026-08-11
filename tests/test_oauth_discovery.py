"""Tests for reusable protected-resource OAuth discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth import OAuthDiscoveryConfig, OAuthProvider, oauth_runtime_bootstrapper

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
        runtime_bootstrapper=oauth_runtime_bootstrapper(OAuthDiscoveryConfig(resource="https://superset.example.test")),
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
    }
