"""Tests for the built-in Microsoft 365 OAuth provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.config.main import Config
from mindroom.oauth.credential_lifecycle import oauth_credentials_have_required_scopes
from mindroom.oauth.microsoft import microsoft_365_oauth_provider, normalize_tenant_id
from mindroom.oauth.providers import OAuthClientConfig, OAuthProviderError
from mindroom.oauth.registry import load_oauth_providers
from tests.microsoft_graph_test_support import runtime_paths, save_client_config

if TYPE_CHECKING:
    from pathlib import Path


def test_provider_declares_a_requester_scoped_graph_contract() -> None:
    """Tokens stay with the requester, and only offline access plus file access are requested."""
    provider = microsoft_365_oauth_provider()

    assert provider.id == "microsoft_365"
    assert provider.display_name == "Microsoft 365"
    assert provider.scopes == ("offline_access", "Files.ReadWrite.All")
    assert provider.credential_service == "microsoft_365_oauth"
    assert provider.tool_config_service == "microsoft_365"
    assert provider.client_config_services == ("microsoft_365_oauth_client",)
    assert provider.requester_scoped_credentials is True
    assert provider.redirect_path == "/api/oauth/microsoft_365/callback"


def test_builtin_registry_exposes_the_provider(tmp_path: Path) -> None:
    """The provider is available without a plugin."""
    paths = runtime_paths(tmp_path)
    providers = load_oauth_providers(Config.model_validate({}, context={"runtime_paths": paths}), paths)

    assert providers["microsoft_365"].credential_service == "microsoft_365_oauth"


@pytest.mark.parametrize(
    ("stored_tenant", "expected_tenant"),
    [
        (None, "organizations"),
        ("", "organizations"),
        ("Contoso.onmicrosoft.com", "contoso.onmicrosoft.com"),
        ("00000000-0000-4000-8000-00000000abcd", "00000000-0000-4000-8000-00000000abcd"),
        ("common", "common"),
    ],
)
def test_authorization_url_uses_the_configured_tenant(
    tmp_path: Path,
    stored_tenant: str | None,
    expected_tenant: str,
) -> None:
    """The stored app registration's tenant selects the v2 authorize and token endpoints."""
    paths = runtime_paths(tmp_path)
    save_client_config(paths, **({"tenant_id": stored_tenant} if stored_tenant is not None else {}))
    provider = microsoft_365_oauth_provider()

    url = asyncio.run(provider.authorization_uri_async(paths, state="opaque-state"))
    endpoints = asyncio.run(provider.runtime_endpoints(paths))

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    authority = f"https://login.microsoftonline.com/{expected_tenant}/oauth2/v2.0"
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{authority}/authorize"
    assert endpoints.token_url == f"{authority}/token"
    assert query["client_id"] == ["entra-client"]
    assert query["redirect_uri"] == ["https://chat.example.com/api/oauth/microsoft_365/callback"]
    assert query["scope"] == ["offline_access Files.ReadWrite.All"]
    assert query["state"] == ["opaque-state"]


@pytest.mark.parametrize("tenant", ["consumers", "../evil", "a b", "contoso", "x.-bad.com", "http://x.com"])
def test_invalid_tenants_are_rejected(tmp_path: Path, tenant: str) -> None:
    """Personal-account and malformed tenants never reach an endpoint URL."""
    paths = runtime_paths(tmp_path)
    save_client_config(paths, tenant_id=tenant)
    with pytest.raises(OAuthProviderError, match="tenant_id"):
        asyncio.run(microsoft_365_oauth_provider().runtime_endpoints(paths))
    with pytest.raises(OAuthProviderError):
        normalize_tenant_id(tenant)


@pytest.mark.parametrize(
    "granted",
    [
        "Files.ReadWrite.All openid profile",
        "https://graph.microsoft.com/Files.ReadWrite.All https://graph.microsoft.com/User.Read",
        "files.readwrite.all",
    ],
)
def test_token_parser_normalizes_graph_scope_spelling(tmp_path: Path, granted: str) -> None:
    """Graph may report scopes with its resource prefix or other casing; stored scopes use the requested names."""
    paths = runtime_paths(tmp_path)
    provider = microsoft_365_oauth_provider()
    assert provider.token_parser is not None
    result = provider.token_parser(
        provider,
        {"access_token": "token", "refresh_token": "refresh", "expires_in": 3600, "scope": granted},
        OAuthClientConfig(client_id="entra-client", client_secret="entra-secret", redirect_uri="https://x/cb"),  # noqa: S106
        paths,
    )

    assert "Files.ReadWrite.All" in result.token_data["scopes"]
    assert oauth_credentials_have_required_scopes(provider, result.token_data)


def test_token_parser_does_not_invent_missing_scopes(tmp_path: Path) -> None:
    """A grant without file access stays insufficient after normalization."""
    paths = runtime_paths(tmp_path)
    provider = microsoft_365_oauth_provider()
    assert provider.token_parser is not None
    result = provider.token_parser(
        provider,
        {"access_token": "token", "refresh_token": "refresh", "scope": "Files.Read User.Read"},
        OAuthClientConfig(client_id="entra-client", client_secret="entra-secret", redirect_uri="https://x/cb"),  # noqa: S106
        paths,
    )

    assert not oauth_credentials_have_required_scopes(provider, result.token_data)
