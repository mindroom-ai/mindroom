"""Tests for the built-in Atlassian Cloud OAuth 2.0 (3LO) provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.atlassian import AtlassianProduct, atlassian_oauth_provider
from mindroom.oauth.registry import load_oauth_providers

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://chat.example.com"},
    )


def test_default_provider_declares_per_user_atlassian_contract() -> None:
    """The built-in provider uses Atlassian 3LO endpoints and requester-only token storage."""
    provider = atlassian_oauth_provider()

    assert provider.id == "atlassian"
    assert provider.display_name == "Atlassian"
    assert provider.authorization_url == "https://auth.atlassian.com/authorize"
    assert provider.token_url == "https://auth.atlassian.com/oauth/token"  # noqa: S105
    assert provider.credential_service == "atlassian_oauth"
    assert provider.tool_config_service == "atlassian"
    assert provider.client_config_services == ()
    assert provider.shared_client_config_services == ("atlassian_oauth_client",)
    assert provider.requester_scoped_credentials is True
    assert dict(provider.extra_auth_params) == {"audience": "api.atlassian.com", "prompt": "consent"}
    assert provider.redirect_path == "/api/oauth/atlassian/callback"


def test_default_provider_requests_minimal_scopes_for_every_function() -> None:
    """Classic scopes cover everything except Confluence writes, which exist only as v2 granular scopes."""
    assert atlassian_oauth_provider().scopes == (
        "offline_access",
        "read:jira-work",
        "write:jira-work",
        "search:confluence",
        "read:confluence-content.all",
        "readonly:content.attachment:confluence",
        "read:space:confluence",
        "write:page:confluence",
        "write:comment:confluence",
    )


@pytest.mark.parametrize(
    ("products", "write", "expected"),
    [
        (("jira",), False, ("offline_access", "read:jira-work")),
        (("jira",), True, ("offline_access", "read:jira-work", "write:jira-work")),
        (
            ("confluence",),
            False,
            (
                "offline_access",
                "search:confluence",
                "read:confluence-content.all",
                "readonly:content.attachment:confluence",
            ),
        ),
    ],
)
def test_scopes_follow_enabled_products_and_write_access(
    products: tuple[AtlassianProduct, ...],
    write: bool,
    expected: tuple[str, ...],
) -> None:
    """Connections request only the scopes their enabled functions call."""
    assert atlassian_oauth_provider(products=products, write=write).scopes == expected


def test_builtin_registry_exposes_atlassian_provider(tmp_path: Path) -> None:
    """The built-in provider is available without a plugin."""
    paths = _runtime_paths(tmp_path)
    providers = load_oauth_providers(Config.model_validate({}, context={"runtime_paths": paths}), paths)

    assert providers["atlassian"].credential_service == "atlassian_oauth"


def test_authorization_url_carries_audience_consent_and_callback(tmp_path: Path) -> None:
    """Atlassian requires the API audience and consent prompt on every authorization request."""
    paths = _runtime_paths(tmp_path)
    get_runtime_credentials_manager(paths).save_credentials(
        "atlassian_oauth_client",
        {"client_id": "atlassian-client", "client_secret": "atlassian-secret"},
    )
    provider = atlassian_oauth_provider()

    url = asyncio.run(provider.authorization_uri_async(paths, state="opaque-state"))

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.atlassian.com/authorize"
    assert query["audience"] == ["api.atlassian.com"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["atlassian-client"]
    assert query["state"] == ["opaque-state"]
    assert query["redirect_uri"] == ["https://chat.example.com/api/oauth/atlassian/callback"]
    assert query["scope"] == [" ".join(provider.scopes)]
