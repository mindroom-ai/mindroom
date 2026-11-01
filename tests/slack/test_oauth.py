"""Tests for the Slack user OAuth provider."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.providers import OAuthClaimValidationError, OAuthProviderError
from mindroom.slack import app as slack_app
from mindroom.slack import oauth as slack_oauth
from mindroom.slack.app import SlackAppCredentials, slack_bot_identity
from mindroom.slack.oauth import SLACK_USER_SCOPES, slack_oauth_provider
from mindroom.tool_system.plugins import config_plugins
from tests.oauth_test_utils import oauth_authorization_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.oauth.providers import OAuthTokenResult

_BOT_TOKEN = "xoxb-test-token"  # noqa: S105
_SIGNING_SECRET = "signing-secret"  # noqa: S105
_USER_TOKEN = "xoxp-user-token"  # noqa: S105
_AUTH_TEST_BODY = {
    "ok": True,
    "url": "https://example.slack.com/",
    "team": "Example",
    "user": "mindroom",
    "team_id": "T0123ABCD",
    "user_id": "U0123BOT0",
}
_USER_SCOPE = "search:read,channels:history,channels:read"
# What `oauth.v2.access` answers when only `user_scope` was requested: no top-level `access_token`.
_EXCHANGE_BODY: dict[str, Any] = {
    "ok": True,
    "app_id": "A0123ABCD",
    "authed_user": {
        "id": "U0123ABCD",
        "scope": _USER_SCOPE,
        "access_token": _USER_TOKEN,
        "token_type": "user",
    },
    "team": {"id": "T0123ABCD", "name": "Example"},
    "enterprise": None,
    "is_enterprise_install": False,
}
_OTHER_TEAM_BODY = {**_EXCHANGE_BODY, "team": {"id": "T0OTHER99", "name": "Other"}}


@pytest.fixture(autouse=True)
def _empty_identity_cache() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    yield
    slack_app._IDENTITIES.clear()


@pytest.fixture
def auth_test_calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Answer the bot's `auth.test` for the provider and record each request."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_AUTH_TEST_BODY)

    monkeypatch.setattr(slack_oauth, "slack_http_client", lambda: _mock_client(handler))
    return calls


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10.0)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://mindroom.example.test"},
    )


def _configure(runtime_paths: RuntimePaths) -> None:
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("slack_oauth_client", {"client_id": "slack-client-id", "client_secret": "slack-secret"})
    manager.save_credentials("slack_app", {"bot_token": _BOT_TOKEN, "signing_secret": _SIGNING_SECRET})


def _cache_bot_identity() -> None:
    async def lookup() -> None:
        async with _mock_client(lambda _request: httpx.Response(200, json=_AUTH_TEST_BODY)) as client:
            await slack_bot_identity(client, SlackAppCredentials(_BOT_TOKEN, _SIGNING_SECRET, None))

    asyncio.run(lookup())


def _parse(runtime_paths: RuntimePaths, payload: dict[str, Any]) -> OAuthTokenResult:
    provider = slack_oauth_provider()
    client_config = provider.client_config(runtime_paths)
    assert client_config is not None
    return slack_oauth._parse_slack_token(provider, payload, client_config, runtime_paths)


def _exchange(runtime_paths: RuntimePaths) -> OAuthTokenResult:
    provider = slack_oauth_provider()
    return asyncio.run(provider.exchange_code("authorization-code", runtime_paths, token_url=provider.token_url))


def _serve_token_endpoint(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> list[httpx.Request]:
    """Answer the code exchange through the real OAuth client and record the request."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    class _MockTransportClient(AsyncOAuth2Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _MockTransportClient)
    return requests


def test_provider_declares_slack_user_oauth_contract() -> None:
    provider = slack_oauth_provider()

    assert provider.id == "slack"
    assert provider.display_name == "Slack"
    assert provider.authorization_url == "https://slack.com/oauth/v2/authorize"
    assert provider.token_url == "https://slack.com/api/oauth.v2.access"  # noqa: S105
    assert provider.scopes == ()
    assert provider.allow_empty_scopes is True
    assert provider.extra_auth_params == {"user_scope": ",".join(SLACK_USER_SCOPES)}
    assert provider.credential_service == "slack_oauth"
    assert provider.tool_config_service == "slack"
    assert provider.client_config_services == ("slack_oauth_client",)
    assert provider.redirect_path == "/api/oauth/slack/callback"
    assert provider.requester_scoped_credentials is True
    assert provider.runtime_bootstrapper is not None
    assert provider.token_parser is not None
    assert provider.account_claims_reader is None
    assert provider.claim_validator is None


def test_parse_code_exchange_shape(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    _cache_bot_identity()

    result = _parse(runtime_paths, _EXCHANGE_BODY)

    assert result.token_data == {"token": _USER_TOKEN, "scopes": ["search:read", "channels:history", "channels:read"]}
    assert result.claims == {"sub": "U0123ABCD", "team_id": "T0123ABCD"}
    assert result.claims_verified is True


def test_missing_user_token_raises(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    _cache_bot_identity()

    with pytest.raises(OAuthProviderError, match="Slack returned no user token"):
        _parse(runtime_paths, {**_EXCHANGE_BODY, "authed_user": {"id": "U0123ABCD", "scope": _USER_SCOPE}})
    with pytest.raises(OAuthProviderError, match="Slack returned no user token"):
        _parse(runtime_paths, {key: value for key, value in _EXCHANGE_BODY.items() if key != "authed_user"})


def test_parse_rejects_other_team(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    _cache_bot_identity()

    # A claim validation error is the kind the OAuth callback shows the person as it is.
    with pytest.raises(OAuthClaimValidationError, match="This Slack account belongs to another workspace"):
        _parse(runtime_paths, _OTHER_TEAM_BODY)


def test_parse_requires_configured_app(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)

    # The app is stored but its identity was never looked up.
    with pytest.raises(OAuthProviderError, match="The MindRoom Slack app is not configured"):
        _parse(runtime_paths, _EXCHANGE_BODY)

    _cache_bot_identity()
    get_runtime_credentials_manager(runtime_paths).delete_credentials("slack_app")

    with pytest.raises(OAuthProviderError, match="The MindRoom Slack app is not configured"):
        _parse(runtime_paths, _EXCHANGE_BODY)


@pytest.mark.usefixtures("auth_test_calls")
def test_scopes_requested_as_user_scope_only(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)

    authorization_url = asyncio.run(
        oauth_authorization_url(slack_oauth_provider(), runtime_paths, state="opaque-state"),
    )
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query, keep_blank_values=True)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://slack.com/oauth/v2/authorize"
    assert query["client_id"] == ["slack-client-id"]
    assert query["state"] == ["opaque-state"]
    assert query["user_scope"] == [",".join(SLACK_USER_SCOPES)]
    assert "scope" not in query


def test_runtime_endpoints_look_up_the_bot_once(tmp_path: Path, auth_test_calls: list[httpx.Request]) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    provider = slack_oauth_provider()

    first = asyncio.run(provider.runtime_endpoints(runtime_paths))
    second = asyncio.run(provider.runtime_endpoints(runtime_paths))

    assert first == second
    assert first.authorization_url == provider.authorization_url
    assert first.token_url == provider.token_url
    assert len(auth_test_calls) == 1
    assert auth_test_calls[0].headers["Authorization"] == f"Bearer {_BOT_TOKEN}"


@pytest.mark.usefixtures("auth_test_calls")
def test_full_exchange_reads_the_user_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    requests = _serve_token_endpoint(monkeypatch, _EXCHANGE_BODY)

    result = _exchange(runtime_paths)

    assert result.token_data["token"] == _USER_TOKEN
    assert result.token_data["scopes"] == ["search:read", "channels:history", "channels:read"]
    assert result.token_data["client_id"] == "slack-client-id"
    assert result.token_data["token_uri"] == "https://slack.com/api/oauth.v2.access"  # noqa: S105
    assert result.token_data["_oauth_provider"] == "slack"
    assert result.claims == {"sub": "U0123ABCD", "team_id": "T0123ABCD"}
    assert result.claims_verified is True
    (request,) = requests
    body = parse_qs(request.content.decode())
    assert body["code"] == ["authorization-code"]
    assert body["grant_type"] == ["authorization_code"]
    assert "scope" not in body
    assert "user_scope" not in body


@pytest.mark.usefixtures("auth_test_calls")
def test_full_exchange_rejects_other_team(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)

    with (
        patch(
            "authlib.integrations.httpx_client.AsyncOAuth2Client.fetch_token",
            new=AsyncMock(return_value=_OTHER_TEAM_BODY),
        ),
        pytest.raises(OAuthClaimValidationError, match="This Slack account belongs to another workspace"),
    ):
        _exchange(runtime_paths)

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("slack_oauth") is None


@pytest.mark.usefixtures("auth_test_calls")
def test_full_exchange_surfaces_a_slack_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _configure(runtime_paths)
    _serve_token_endpoint(monkeypatch, {"ok": False, "error": "invalid_code"})

    with pytest.raises(OAuthProviderError, match="OAuth token exchange failed"):
        _exchange(runtime_paths)


def test_provider_registered(tmp_path: Path) -> None:
    providers = config_plugins(Config(), _runtime_paths(tmp_path)).oauth_providers

    assert providers["slack"].credential_service == "slack_oauth"
    assert providers["slack"].tool_config_service == "slack"
