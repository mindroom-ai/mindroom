"""Tests for the built-in Smartsheet OAuth provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.smartsheet import smartsheet_oauth_provider
from mindroom.tool_system.plugins import config_plugins
from tests.oauth_test_utils import oauth_authorization_url

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://chat.example.com"},
    )


def test_provider_declares_per_user_smartsheet_contract() -> None:
    """The provider uses Smartsheet's endpoints, requester-only tokens, and the access token as manual fallback."""
    provider = smartsheet_oauth_provider()

    assert provider.id == "smartsheet"
    assert provider.display_name == "Smartsheet"
    assert provider.authorization_url == "https://app.smartsheet.com/b/authorize"
    assert provider.token_url == "https://api.smartsheet.com/2.0/token"  # noqa: S105
    assert provider.scopes == ("READ_SHEETS", "WRITE_SHEETS")
    assert provider.credential_service == "smartsheet_oauth"
    assert provider.tool_config_service == "smartsheet"
    assert provider.client_config_services == ("smartsheet_oauth_client",)
    assert provider.requester_scoped_credentials is True
    assert provider.tool_config_oauth_fallback_fields == ("access_token",)
    assert provider.token_endpoint_auth_method == "client_secret_post"  # noqa: S105
    assert provider.redirect_path == "/api/oauth/smartsheet/callback"


def test_builtin_registry_exposes_smartsheet_provider(tmp_path: Path) -> None:
    """The built-in provider is available without a plugin."""
    paths = _runtime_paths(tmp_path)
    providers = config_plugins(Config.model_validate({}, context={"runtime_paths": paths}), paths).oauth_providers

    assert providers["smartsheet"].credential_service == "smartsheet_oauth"


def test_authorization_url_carries_scopes_and_callback(tmp_path: Path) -> None:
    """The authorization request asks for sheet read and write scopes and names MindRoom's callback."""
    paths = _runtime_paths(tmp_path)
    get_runtime_credentials_manager(paths).save_credentials(
        "smartsheet_oauth_client",
        {"client_id": "smartsheet-client", "client_secret": "smartsheet-secret"},
    )

    url = asyncio.run(oauth_authorization_url(smartsheet_oauth_provider(), paths, state="opaque-state"))

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://app.smartsheet.com/b/authorize"
    assert query["scope"] == ["READ_SHEETS WRITE_SHEETS"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["smartsheet-client"]
    assert query["state"] == ["opaque-state"]
    assert query["redirect_uri"] == ["https://chat.example.com/api/oauth/smartsheet/callback"]
