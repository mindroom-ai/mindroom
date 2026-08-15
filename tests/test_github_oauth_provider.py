"""Tests for the built-in GitHub App OAuth provider."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext, refresh_oauth_credentials
from mindroom.oauth.github import github_oauth_provider

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.oauth.providers import OAuthProvider


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )


def _provider() -> OAuthProvider:
    return github_oauth_provider()


def _save_client_config(runtime_paths: RuntimePaths) -> None:
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "github_oauth_client",
        {
            "client_id": "github-client-id",
            "client_secret": "github-client-secret",
        },
    )


def test_github_provider_declares_github_app_user_oauth_contract() -> None:
    provider = _provider()

    assert provider.id == "github"
    assert provider.display_name == "GitHub"
    assert provider.authorization_url == "https://github.com/login/oauth/authorize"
    assert provider.token_url == "https://github.com/login/oauth/access_token"  # noqa: S105
    assert provider.scopes == ()
    assert provider.allow_empty_scopes is True
    assert provider.pkce_code_challenge_method == "S256"
    assert provider.credential_service == "github_oauth"
    assert provider.tool_config_service == "github"
    assert provider.client_config_services == ("github_oauth_client",)
    assert provider.redirect_path == "/api/oauth/github/callback"
    assert provider.requester_scoped_credentials is True
    assert provider.tool_config_oauth_fallback_fields == ("access_token",)
    assert provider.tool_config_oauth_fallback_env_vars == ("GITHUB_ACCESS_TOKEN",)


def test_github_authorization_url_omits_classic_oauth_scope(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)

    authorization_url = asyncio.run(
        _provider().authorization_uri_async(
            runtime_paths,
            state="opaque-state",
            code_verifier="v" * 64,
        ),
    )
    query = parse_qs(urlparse(authorization_url).query, keep_blank_values=True)

    assert query["client_id"] == ["github-client-id"]
    assert query["state"] == ["opaque-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query


def test_github_token_exchange_normalizes_rotating_user_token(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    token_response = {
        "access_token": "github-user-access",
        "refresh_token": "github-user-refresh",
        "expires_in": 28_800,
        "refresh_token_expires_in": 15_897_600,
        "scope": "",
        "token_type": "bearer",
    }

    with patch(
        "mindroom.oauth.providers.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=token_response),
    ):
        result = asyncio.run(
            _provider().exchange_code(
                "authorization-code",
                runtime_paths,
                code_verifier="v" * 64,
            ),
        )

    assert result.token_data["token"] == "github-user-access"  # noqa: S105
    assert result.token_data["refresh_token"] == "github-user-refresh"  # noqa: S105
    assert result.token_data["client_id"] == "github-client-id"
    assert result.token_data["scopes"] == []
    assert result.token_data["token_type"] == "bearer"  # noqa: S105
    assert result.token_data["expires_at"] > time.time()
    assert result.token_data["_oauth_provider"] == "github"


def test_github_refresh_persists_rotated_access_and_refresh_tokens(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    credentials_manager.save_credentials(
        "github_oauth",
        {
            "token": "old-access",
            "refresh_token": "old-refresh",
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": 1.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
    )
    refresh_response = {
        "access_token": "rotated-access",
        "refresh_token": "rotated-refresh",
        "expires_in": 28_800,
        "refresh_token_expires_in": 15_897_600,
        "scope": "",
        "token_type": "bearer",
    }

    with patch(
        "mindroom.oauth.providers.AsyncOAuth2Client.refresh_token",
        new=AsyncMock(return_value=refresh_response),
    ):
        refreshed = asyncio.run(
            refresh_oauth_credentials(
                OAuthCredentialContext(
                    provider=_provider(),
                    runtime_paths=runtime_paths,
                    credentials_manager=credentials_manager,
                    worker_target=None,
                ),
            ),
        )

    stored = credentials_manager.load_credentials("github_oauth")
    assert refreshed is not None
    assert refreshed["token"] == "rotated-access"  # noqa: S105
    assert refreshed["refresh_token"] == "rotated-refresh"  # noqa: S105
    assert stored == refreshed
