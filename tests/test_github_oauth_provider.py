"""Tests for the built-in GitHub App OAuth provider."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.common.errors import AuthlibBaseError

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth import github as github_oauth
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot,
    oauth_account_label,
    refresh_oauth_credentials,
)
from mindroom.oauth.github import GITHUB_USER_API_URL, LOGIN_LOOKUP_TIMEOUT_SECONDS, github_oauth_provider
from mindroom.oauth.providers import OAuthRefreshRejectedError
from tests.oauth_test_utils import oauth_authorization_url, publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.oauth.providers import OAuthProvider


_REAL_LOGIN_LOOKUP_CLIENT = github_oauth._login_lookup_client


@pytest.fixture(autouse=True)
def login_lookups(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Keep every test off the network: the GitHub login lookup is refused unless a test answers it."""
    lookups: list[httpx.Request] = []
    _answer_login_lookup(monkeypatch, lookups, lambda _request: httpx.Response(503))
    return lookups


def _answer_login_lookup(
    monkeypatch: pytest.MonkeyPatch,
    lookups: list[httpx.Request],
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def record(request: httpx.Request) -> httpx.Response:
        lookups.append(request)
        return handler(request)

    monkeypatch.setattr(
        github_oauth,
        "_login_lookup_client",
        lambda: httpx.Client(transport=httpx.MockTransport(record), timeout=LOGIN_LOOKUP_TIMEOUT_SECONDS),
    )


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
        oauth_authorization_url(
            _provider(),
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
        "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=token_response),
    ):
        result = asyncio.run(
            _provider().exchange_code(
                "authorization-code",
                runtime_paths,
                token_url=_provider().token_url,
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
    publish_oauth_credentials(
        _provider(),
        {
            "token": "old-access",
            "refresh_token": "old-refresh",
            "token_uri": _provider().token_url,
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": 1.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    refresh_response = {
        "access_token": "rotated-access",
        "refresh_token": "rotated-refresh",
        "expires_in": 28_800,
        "refresh_token_expires_in": 15_897_600,
        "scope": "",
        "token_type": "bearer",
    }

    context = OAuthCredentialContext(
        provider=_provider(),
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token",
        new=AsyncMock(return_value=refresh_response),
    ):
        refreshed = asyncio.run(refresh_oauth_credentials(context))

    stored = asyncio.run(load_oauth_credentials_snapshot(context)).credentials
    assert refreshed is not None
    assert refreshed["token"] == "rotated-access"  # noqa: S105
    assert refreshed["refresh_token"] == "rotated-refresh"  # noqa: S105
    assert stored == refreshed


def test_github_bad_refresh_token_is_terminal_and_deletes_credentials(tmp_path: Path) -> None:
    """GitHub's structured terminal code must invalidate credentials without leaking its description."""
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    context = OAuthCredentialContext(
        provider=_provider(),
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    publish_oauth_credentials(
        context.provider,
        {
            "token": "old-access",
            "refresh_token": "old-refresh",
            "token_uri": context.provider.token_url,
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": 1.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    leaked_description = "provider-controlled account detail"

    with (
        patch(
            "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token",
            new=AsyncMock(
                side_effect=AuthlibBaseError(
                    error="bad_refresh_token",
                    description=leaked_description,
                ),
            ),
        ),
        pytest.raises(OAuthRefreshRejectedError) as exc_info,
    ):
        asyncio.run(refresh_oauth_credentials(context))

    assert exc_info.value.oauth_error == "bad_refresh_token"
    assert leaked_description not in str(exc_info.value)
    assert asyncio.run(load_oauth_credentials_snapshot(context)).credentials is None


_TOKEN_RESPONSE = {
    "access_token": "github-user-access",
    "refresh_token": "github-user-refresh",
    "expires_in": 28_800,
    "scope": "",
    "token_type": "bearer",
}


def _exchange(runtime_paths: RuntimePaths) -> object:
    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=_TOKEN_RESPONSE),
    ):
        return asyncio.run(
            _provider().exchange_code(
                "authorization-code",
                runtime_paths,
                token_url=_provider().token_url,
                code_verifier="v" * 64,
            ),
        )


def test_github_login_lookup_targets_the_user_api_with_a_five_second_timeout() -> None:
    assert GITHUB_USER_API_URL == "https://api.github.com/user"
    assert LOGIN_LOOKUP_TIMEOUT_SECONDS == 5.0
    client = _REAL_LOGIN_LOOKUP_CLIENT()
    try:
        assert isinstance(client, httpx.Client)
        assert client.timeout == httpx.Timeout(5.0)
        assert client.follow_redirects is False
    finally:
        client.close()


def test_github_login_lookup_timeout_still_connects_the_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_lookups: list[httpx.Request],
) -> None:
    """A GitHub that times out costs the account its label, never the connection."""
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)

    def times_out(request: httpx.Request) -> httpx.Response:
        message = "read timed out"
        raise httpx.ReadTimeout(message, request=request)

    _answer_login_lookup(monkeypatch, login_lookups, times_out)

    result = _exchange(runtime_paths)

    assert len(login_lookups) == 1
    assert result.token_data["token"] == "github-user-access"  # noqa: S105
    assert result.claims == {}


def test_a_claims_reader_that_raises_still_lets_the_account_connect(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reader is best effort by contract; one that raises anyway costs the label, never the connection."""
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)

    def raises(token_data: object) -> dict[str, object]:
        msg = f"secret {token_data}"
        raise RuntimeError(msg)

    provider = replace(_provider(), account_claims_reader=raises)
    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
        new=AsyncMock(return_value=_TOKEN_RESPONSE),
    ):
        result = asyncio.run(
            provider.exchange_code(
                "authorization-code",
                runtime_paths,
                token_url=provider.token_url,
                code_verifier="v" * 64,
            ),
        )

    assert result.token_data["token"] == "github-user-access"  # noqa: S105
    assert result.claims == {}
    assert result.claims_verified is False
    assert "github-user-access" not in caplog.text


def test_github_exchange_reads_the_login_with_the_new_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_lookups: list[httpx.Request],
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    _answer_login_lookup(
        monkeypatch,
        login_lookups,
        lambda _request: httpx.Response(200, json={"login": "octo", "id": 583231, "email": "private@example.org"}),
    )

    result = _exchange(runtime_paths)

    assert [(str(r.url), r.headers["authorization"]) for r in login_lookups] == [
        ("https://api.github.com/user", "Bearer github-user-access"),
    ]
    assert result.claims == {"login": "octo"}
    assert result.claims_verified is True
    stored = _provider().token_result_with_safe_claims(result).token_data
    assert stored["_oauth_claims"] == {"login": "octo"}
    assert stored["_oauth_claims_verified"] is True
    assert oauth_account_label(stored) == "octo"
    assert all(value != "octo" for key, value in stored.items() if key != "_oauth_claims")


@pytest.mark.parametrize(
    "answer",
    [
        lambda _request: httpx.Response(500),
        lambda _request: httpx.Response(200, text="<html>"),
        lambda _request: httpx.Response(200, json={"login": ""}),
        lambda _request: httpx.Response(200, json={"login": 7}),
        lambda _request: httpx.Response(200, json={"login": "x" * 40}),
        lambda _request: httpx.Response(200, json={"login": "-octo"}),
        lambda _request: httpx.Response(200, json=["octo"]),
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
    ],
    ids=["server_error", "not_json", "empty", "not_text", "too_long", "bad_start", "not_object", "timeout"],
)
def test_github_exchange_without_a_login_still_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_lookups: list[httpx.Request],
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    _answer_login_lookup(monkeypatch, login_lookups, answer)

    result = _exchange(runtime_paths)

    assert result.token_data["token"] == "github-user-access"  # noqa: S105
    assert result.claims == {}
    assert result.claims_verified is False
    assert oauth_account_label(_provider().token_result_with_safe_claims(result).token_data) is None


def test_github_refresh_keeps_the_stored_login_and_never_looks_it_up_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_lookups: list[httpx.Request],
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _save_client_config(runtime_paths)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    publish_oauth_credentials(
        _provider(),
        {
            "token": "old-access",
            "refresh_token": "old-refresh",
            "token_uri": _provider().token_url,
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": 1.0,
            "_source": "oauth",
            "_oauth_provider": "github",
            "_oauth_claims": {"login": "octo"},
            "_oauth_claims_verified": True,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    _answer_login_lookup(monkeypatch, login_lookups, lambda _request: httpx.Response(200, json={"login": "x"}))
    context = OAuthCredentialContext(
        provider=_provider(),
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    with patch(
        "authlib.integrations.httpx_client.AsyncOAuth2Client.refresh_token",
        new=AsyncMock(return_value={**_TOKEN_RESPONSE, "access_token": "rotated-access"}),
    ):
        refreshed = asyncio.run(refresh_oauth_credentials(context))

    assert refreshed is not None
    assert refreshed["token"] == "rotated-access"  # noqa: S105
    assert oauth_account_label(refreshed) == "octo"
    assert login_lookups == []
