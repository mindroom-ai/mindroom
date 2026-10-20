"""Tests for the built-in Microsoft 365 OAuth providers."""
# ruff: noqa: D103, S105

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth import microsoft as microsoft_oauth
from mindroom.oauth.credential_lifecycle import oauth_account_label
from mindroom.oauth.microsoft import (
    ACCOUNT_LOOKUP_TIMEOUT_SECONDS,
    GRAPH_ME_URL,
    microsoft_files_oauth_provider,
)
from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REAL_ACCOUNT_LOOKUP_CLIENT = microsoft_oauth._account_lookup_client
_TOKEN_RESPONSE = {
    "access_token": "graph-access",
    "refresh_token": "graph-refresh",
    "expires_in": 3600,
    "scope": "Files.ReadWrite.All User.Read",
    "token_type": "Bearer",
}


@pytest.fixture(autouse=True)
def account_lookups(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Keep every test off the network: the account lookup is refused unless a test answers it."""
    lookups: list[httpx.Request] = []
    _answer_account_lookup(monkeypatch, lookups, lambda _request: httpx.Response(503))
    return lookups


def _answer_account_lookup(
    monkeypatch: pytest.MonkeyPatch,
    lookups: list[httpx.Request],
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def record(request: httpx.Request) -> httpx.Response:
        lookups.append(request)
        return handler(request)

    monkeypatch.setattr(
        microsoft_oauth,
        "_account_lookup_client",
        lambda: httpx.Client(transport=httpx.MockTransport(record), timeout=ACCOUNT_LOOKUP_TIMEOUT_SECONDS),
    )


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test", **env},
    )


def _save_shared_client_config(runtime_paths: RuntimePaths) -> None:
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "microsoft_oauth_client",
        {
            "client_id": "microsoft-client-id",
            "client_secret": "microsoft-client-secret",
            "redirect_uri": "https://mindroom.example.test/api/oauth/other/callback",
        },
    )


def _exchange(runtime_paths: RuntimePaths) -> object:
    provider = microsoft_files_oauth_provider()
    token_url = asyncio.run(provider.runtime_endpoints(runtime_paths)).token_url
    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=_TOKEN_RESPONSE),
    ):
        return asyncio.run(
            provider.exchange_code("authorization-code", runtime_paths, token_url=token_url, code_verifier="v" * 64),
        )


def test_files_provider_declares_a_requester_scoped_graph_contract() -> None:
    provider = microsoft_files_oauth_provider()

    assert provider.id == "microsoft_files"
    assert provider.display_name == "Microsoft OneDrive and SharePoint"
    assert provider.scopes == ("Files.ReadWrite.All", "User.Read", "offline_access")
    assert provider.credential_service == "microsoft_files_oauth"
    assert provider.tool_config_service == "microsoft_files"
    assert provider.client_config_services == ("microsoft_files_oauth_client",)
    assert provider.shared_client_config_services == ("microsoft_oauth_client",)
    assert provider.token_endpoint_auth_method == "client_secret_post"
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.requester_scoped_credentials is True


def test_endpoints_default_to_the_organizations_tenant(tmp_path: Path) -> None:
    endpoints = asyncio.run(microsoft_files_oauth_provider().runtime_endpoints(_runtime_paths(tmp_path)))

    assert endpoints.authorization_url == "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
    assert endpoints.token_url == "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"


def test_endpoints_use_the_configured_tenant(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path, MICROSOFT_TENANT_ID="contoso.onmicrosoft.com")

    endpoints = asyncio.run(microsoft_files_oauth_provider().runtime_endpoints(runtime_paths))

    assert (
        endpoints.authorization_url == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/authorize"
    )
    assert endpoints.token_url == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"


@pytest.mark.parametrize("tenant", ["contoso.com/oauth2", "evil.example?x=1", "-leading", "a b", "x" * 300])
def test_a_tenant_that_is_not_an_id_or_domain_is_refused(tmp_path: Path, tenant: str) -> None:
    runtime_paths = _runtime_paths(tmp_path, MICROSOFT_TENANT_ID=tenant)

    with pytest.raises(OAuthProviderError, match="MICROSOFT_TENANT_ID"):
        asyncio.run(microsoft_files_oauth_provider().runtime_endpoints(runtime_paths))


def test_shared_client_config_keeps_the_provider_callback(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_shared_client_config(runtime_paths)

    config = microsoft_files_oauth_provider().client_config(runtime_paths)

    assert config is not None
    assert config.client_id == "microsoft-client-id"
    assert config.redirect_uri == "https://mindroom.example.test/api/oauth/microsoft_files/callback"


def test_account_lookup_uses_one_bounded_request_without_redirects() -> None:
    assert GRAPH_ME_URL == "https://graph.microsoft.com/v1.0/me"
    assert ACCOUNT_LOOKUP_TIMEOUT_SECONDS == 5.0
    client = _REAL_ACCOUNT_LOOKUP_CLIENT()
    try:
        assert client.timeout == httpx.Timeout(5.0)
        assert client.follow_redirects is False
    finally:
        client.close()


@pytest.mark.parametrize(
    ("profile", "label"),
    [
        ({"mail": "alex@contoso.com", "userPrincipalName": "alex_upn@contoso.com"}, "alex@contoso.com"),
        ({"mail": None, "userPrincipalName": "alex@contoso.com"}, "alex@contoso.com"),
    ],
)
def test_exchange_labels_the_account_from_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_lookups: list[httpx.Request],
    profile: dict[str, object],
    label: str,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_shared_client_config(runtime_paths)
    _answer_account_lookup(monkeypatch, account_lookups, lambda _request: httpx.Response(200, json=profile))

    result = _exchange(runtime_paths)

    [lookup] = account_lookups
    assert lookup.url.copy_with(query=None) == httpx.URL(GRAPH_ME_URL)
    assert lookup.headers["authorization"] == "Bearer graph-access"
    stored = microsoft_files_oauth_provider().token_result_with_safe_claims(result).token_data
    assert oauth_account_label(stored) == label


@pytest.mark.parametrize(
    "answer",
    [
        lambda _request: httpx.Response(500),
        lambda _request: httpx.Response(200, text="<html>"),
        lambda _request: httpx.Response(200, json={"mail": None, "userPrincipalName": "no-at-sign"}),
        lambda _request: httpx.Response(200, json=["alex@contoso.com"]),
    ],
    ids=["server_error", "not_json", "no_email", "not_object"],
)
def test_exchange_without_a_usable_account_still_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_lookups: list[httpx.Request],
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_shared_client_config(runtime_paths)
    _answer_account_lookup(monkeypatch, account_lookups, answer)

    result = _exchange(runtime_paths)

    stored = microsoft_files_oauth_provider().token_result_with_safe_claims(result).token_data
    assert stored["token"] == "graph-access"
    assert oauth_account_label(stored) is None
