"""Tests for the Spotify toolkit's OAuth access token."""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.spotify import SpotifyTools
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError
from mindroom.oauth.service import OAUTH_REFRESH_REJECTED_REASON, OAUTH_RESET_REQUIRED_REASON
from mindroom.oauth.spotify import SPOTIFY_SCOPES, spotify_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import corrupt_oauth_credential_payload, publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_TOKEN_URL = "https://accounts.spotify.com/api/token"  # noqa: S105


def _shared_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return resolve_worker_target("shared", "assistant", execution_identity=identity)


def _runtime(tmp_path: Path) -> tuple[RuntimePaths, CredentialsManager]:
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("spotify_oauth_client", {"client_id": "spotify-client", "client_secret": "spotify-secret"})
    return paths, manager


def _publish_expiring_connection(manager: CredentialsManager) -> None:
    publish_oauth_credentials(
        spotify_oauth_provider(),
        {
            "token": "expired-token",
            "refresh_token": "spotify-refresh",
            "expires_at": time.time() + 5,
            "client_id": "spotify-client",
            "scopes": list(SPOTIFY_SCOPES),
            "token_uri": _TOKEN_URL,
            "_source": "oauth",
            "_oauth_provider": "spotify",
        },
        credentials_manager=manager,
        worker_target=_shared_target(),
    )


def _serve_spotify(monkeypatch: pytest.MonkeyPatch, token_response: httpx.Response) -> list[tuple[str, str]]:
    """Answer Spotify's token endpoint and Web API, recording each request's authorization and body or path."""
    requests: list[tuple[str, str]] = []

    async def token_endpoint(request: httpx.Request) -> httpx.Response:
        requests.append((request.headers["authorization"], request.content.decode()))
        return token_response

    class _TokenClient(AsyncOAuth2Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs, transport=httpx.MockTransport(token_endpoint))

    def web_api(request: httpx.Request) -> httpx.Response:
        requests.append((request.headers["authorization"], request.url.path))
        return httpx.Response(200, json={"id": "listener", "display_name": "Listener"})

    real_client = httpx.Client
    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _TokenClient)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(web_api), **kwargs))
    return requests


def _tool(paths: RuntimePaths, manager: CredentialsManager) -> SpotifyTools:
    return SpotifyTools(runtime_paths=paths, credentials_manager=manager, worker_target=_shared_target())


def test_tool_sends_the_refreshed_oauth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expiring token is refreshed with the client's Basic credentials before the Web API call uses it."""
    paths, manager = _runtime(tmp_path)
    _publish_expiring_connection(manager)
    requests = _serve_spotify(
        monkeypatch,
        httpx.Response(200, json={"access_token": "renewed-token", "token_type": "Bearer", "expires_in": 3600}),
    )

    result = json.loads(_tool(paths, manager).get_current_user())

    assert result["display_name"] == "Listener"
    basic = "Basic " + base64.b64encode(b"spotify-client:spotify-secret").decode()
    (token_request, api_request) = requests
    assert token_request[0] == basic
    assert parse_qs(token_request[1]) == {"grant_type": ["refresh_token"], "refresh_token": ["spotify-refresh"]}
    assert api_request == ("Bearer renewed-token", "/v1/me")


def test_tool_without_a_connection_asks_to_connect(tmp_path: Path) -> None:
    """A scope that never connected Spotify answers with the connect prompt instead of calling Spotify."""
    paths, manager = _runtime(tmp_path)

    with pytest.raises(OAuthConnectionRequired):
        _tool(paths, manager).get_current_user()


def test_tool_whose_refresh_spotify_rejects_asks_to_reconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked refresh token turns into a reconnect prompt."""
    paths, manager = _runtime(tmp_path)
    _publish_expiring_connection(manager)
    _serve_spotify(monkeypatch, httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(OAuthConnectionRequired) as exc_info:
        _tool(paths, manager).get_current_user()

    assert exc_info.value.reason == OAUTH_REFRESH_REJECTED_REASON


def test_tool_whose_refresh_fails_for_now_reports_a_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token endpoint failure a retry may fix is an error, not a reconnect prompt."""
    paths, manager = _runtime(tmp_path)
    _publish_expiring_connection(manager)
    _serve_spotify(monkeypatch, httpx.Response(503, json={"error": "temporarily_unavailable"}))

    with pytest.raises(OAuthProviderError, match="Spotify authorization could not be refreshed") as exc_info:
        _tool(paths, manager).get_current_user()

    assert not isinstance(exc_info.value, OAuthConnectionRequired)


def test_tool_with_an_unreadable_connection_asks_for_a_reset(tmp_path: Path) -> None:
    """A stored connection that cannot be decoded asks the user to reset it."""
    paths, manager = _runtime(tmp_path)
    _publish_expiring_connection(manager)
    (database,) = tmp_path.rglob("spotify_oauth_credentials.sqlite3")
    corrupt_oauth_credential_payload(database, b"not a credential payload")

    with pytest.raises(OAuthConnectionRequired) as exc_info:
        _tool(paths, manager).get_current_user()

    assert exc_info.value.reason == OAUTH_RESET_REQUIRED_REASON
