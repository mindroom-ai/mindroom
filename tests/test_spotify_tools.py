"""Tests for the Spotify toolkit's access token renewal."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.spotify import SpotifyTools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_SPOTIFY_ENV = {"SPOTIFY_CLIENT_ID": "client-id", "SPOTIFY_CLIENT_SECRET": "client-secret"}


def _fake_spotify(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Serve Spotify's token and Web API endpoints through a fake transport that accepts only the renewed token."""
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers["authorization"]))
        if request.url.host == "accounts.spotify.com":
            assert request.content == b"grant_type=refresh_token&refresh_token=spotify-refresh"
            token = {"access_token": "renewed-token", "token_type": "Bearer", "expires_in": 3600}
            return httpx.Response(200, json=token)
        if request.headers["authorization"] != "Bearer renewed-token":
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        return httpx.Response(200, json={"id": "listener", "display_name": "Listener"})

    real_client = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    def post(url: str, **kwargs: object) -> httpx.Response:
        with client() as fake_client:
            return fake_client.post(url, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(httpx, "post", post)
    return requests


def _spotify_tool(
    tmp_path: Path,
    process_env: dict[str, str],
    *,
    worker_target: ResolvedWorkerTarget | None = None,
    allowed_shared_services: frozenset[str] | None = None,
) -> tuple[SpotifyTools, CredentialsManager]:
    manager = CredentialsManager(base_path=tmp_path / "credentials")
    manager.save_credentials(
        "spotify",
        {
            "access_token": "expired-token",
            "refresh_token": "spotify-refresh",
            "expires_at": int(time.time()) + 30,
            "_source": "ui",
        },
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )
    tool = get_tool_by_name(
        "spotify",
        runtime_paths,
        credentials_manager=manager,
        allowed_shared_services=allowed_shared_services,
        worker_target=worker_target,
    )
    assert isinstance(tool, SpotifyTools)
    return tool, manager


def test_tool_renews_an_expiring_token_before_calling_spotify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token that expires within a minute is renewed with the client credentials and saved before the call."""
    tool, manager = _spotify_tool(tmp_path, _SPOTIFY_ENV)
    requests = _fake_spotify(monkeypatch)

    result = json.loads(tool.get_current_user())

    assert result == {"id": "listener", "display_name": "Listener"}
    assert requests == [
        ("https://accounts.spotify.com/api/token", "Basic Y2xpZW50LWlkOmNsaWVudC1zZWNyZXQ="),
        ("https://api.spotify.com/v1/me", "Bearer renewed-token"),
    ]
    saved = manager.load_credentials("spotify")
    assert saved is not None
    assert (saved["access_token"], saved["refresh_token"]) == ("renewed-token", "spotify-refresh")
    assert saved["expires_at"] >= int(time.time()) + 3500


def test_tool_without_the_client_secret_keeps_the_stored_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without SPOTIFY_CLIENT_SECRET no renewal is attempted, so Spotify's expired-token error reaches the model."""
    tool, manager = _spotify_tool(tmp_path, {"SPOTIFY_CLIENT_ID": "client-id"})
    stored = manager.load_credentials("spotify")
    requests = _fake_spotify(monkeypatch)

    result = json.loads(tool.get_current_user())

    assert result == {"error": {"status": 401, "message": "The access token expired"}}
    assert requests == [("https://api.spotify.com/v1/me", "Bearer expired-token")]
    assert manager.load_credentials("spotify") == stored


def test_tool_does_not_renew_a_connection_shared_through_the_worker_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A granted installation-wide connection keeps its token, so the tool never copies it into the agent's store."""
    tool, manager = _spotify_tool(
        tmp_path,
        _SPOTIFY_ENV,
        worker_target=resolve_worker_target("shared", "general", execution_identity=None),
        allowed_shared_services=frozenset({"spotify"}),
    )
    shared_connection = manager.load_credentials("spotify")
    requests = _fake_spotify(monkeypatch)

    tool.get_current_user()

    assert requests == [("https://api.spotify.com/v1/me", "Bearer expired-token")]
    assert manager.for_primary_runtime_agent_scope("general").load_credentials("spotify") is None
    assert manager.load_credentials("spotify") == shared_connection
