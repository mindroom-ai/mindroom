"""The Spotify OAuth provider and the one-time adoption of tokens its former dashboard routes stored."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.oauth_source import oauth_connection_for_display
from mindroom.oauth.credential_lifecycle import (
    load_oauth_credentials_snapshot_sync,
    load_oauth_credentials_status,
    oauth_credentials_usable,
    reset_oauth_credentials_blocking,
    resolve_oauth_credential_context,
)
from mindroom.oauth.registry import _builtin_oauth_providers
from mindroom.oauth.spotify import SPOTIFY_SCOPES, spotify_oauth_provider
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

# The document the former `/api/integrations/spotify/callback` saved, recorded from that writer.
_LEGACY_DOCUMENT = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_spotify_credentials.json").read_text(encoding="utf-8"),
)
_ADOPTED = {
    "token": "BQD-recorded-access-token",
    "refresh_token": "AQB-recorded-refresh-token",
    "expires_at": 1761782400,
    "client_id": "spotify-client",
    "scopes": list(SPOTIFY_SCOPES),
    "token_uri": "https://accounts.spotify.com/api/token",
    "_source": "oauth",
    "_oauth_provider": "spotify",
}


def _runtime(tmp_path: Path, env: dict[str, str] | None = None) -> tuple[RuntimePaths, CredentialsManager]:
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"SPOTIFY_CLIENT_ID": "spotify-client"} if env is None else env,
    )
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("spotify_oauth_client", {"client_id": "spotify-client", "client_secret": "spotify-secret"})
    return paths, manager


def _shared_target() -> ResolvedWorkerTarget:
    return resolve_worker_target("shared", "assistant", execution_identity=None)


def _legacy_store(manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> CredentialsManager:
    """Return the store the former routes wrote the scope's tokens to."""
    return manager if target is None else manager.for_primary_runtime_agent_scope("assistant")


def _context(
    paths: RuntimePaths,
    manager: CredentialsManager,
    target: ResolvedWorkerTarget | None,
) -> OAuthCredentialContext:
    return resolve_oauth_credential_context(spotify_oauth_provider(), paths, manager, target)


_WORKER_SCOPES = [pytest.param(None, id="unscoped"), pytest.param(_shared_target(), id="shared")]


def test_spotify_is_a_registered_oauth_provider_with_the_former_scopes() -> None:
    """The connect flow requests the scopes the former routes requested and stores the client under its own service."""
    (provider,) = [provider for provider in _builtin_oauth_providers() if provider.id == "spotify"]
    assert set(provider.scopes) == {
        "user-read-private",
        "user-read-email",
        "user-read-playback-state",
        "user-read-currently-playing",
        "user-top-read",
        "playlist-read-private",
        "playlist-modify-public",
        "playlist-modify-private",
        "user-modify-playback-state",
    }
    assert provider.client_config_services == ("spotify_oauth_client",)
    assert provider.redirect_path == "/api/oauth/spotify/callback"


@pytest.mark.parametrize("target", _WORKER_SCOPES)
def test_a_legacy_connection_carries_over_into_the_oauth_store(
    tmp_path: Path,
    target: ResolvedWorkerTarget | None,
) -> None:
    """The first read of a scope adopts its old tokens, usable with the same app's client, and drops the old copy."""
    paths, manager = _runtime(tmp_path)
    _legacy_store(manager, target).save_credentials("spotify", _LEGACY_DOCUMENT)
    context = _context(paths, manager, target)

    credentials = load_oauth_credentials_snapshot_sync(context).credentials

    assert credentials == _ADOPTED
    assert oauth_credentials_usable(context.provider, paths, credentials)
    assert _legacy_store(manager, target).load_credentials("spotify") is None


def test_the_conversion_keeps_the_tool_settings_and_runs_once(tmp_path: Path) -> None:
    """Tool settings stay in the old document, and a scope that disconnected never adopts tokens again."""
    paths, manager = _runtime(tmp_path)
    store = _legacy_store(manager, _shared_target())
    store.save_credentials("spotify", {**_LEGACY_DOCUMENT, "default_market": "GB"})
    context = _context(paths, manager, _shared_target())

    assert reset_oauth_credentials_blocking(context)
    assert store.load_credentials("spotify") == {"default_market": "GB", "_source": "ui"}

    store.save_credentials("spotify", _LEGACY_DOCUMENT)
    assert load_oauth_credentials_snapshot_sync(context).credentials is None


def test_a_listing_shows_a_legacy_connection_as_connected(tmp_path: Path) -> None:
    """A listing, which creates no empty stores, adopts a scope's old tokens and reads it as connected."""
    paths, manager = _runtime(tmp_path)
    _legacy_store(manager, _shared_target()).save_credentials("spotify", _LEGACY_DOCUMENT)

    context = _context(paths, manager, _shared_target())
    status = asyncio.run(load_oauth_credentials_status(context, create_store=False))

    assert status.credentials == _ADOPTED
    assert oauth_credentials_usable(context.provider, paths, status.credentials)


def test_a_scope_without_legacy_tokens_is_listed_without_creating_a_store(tmp_path: Path) -> None:
    """A listing of a scope that never connected writes nothing."""
    paths, manager = _runtime(tmp_path)

    status = asyncio.run(load_oauth_credentials_status(_context(paths, manager, _shared_target()), create_store=False))

    assert status.credentials is None
    assert not list(tmp_path.rglob("spotify_oauth_credentials.sqlite3"))


def test_a_legacy_connection_without_a_known_client_needs_a_reconnect(tmp_path: Path) -> None:
    """Without the client the former routes renewed with, the adopted tokens cannot be used."""
    paths, manager = _runtime(tmp_path, env={})
    _legacy_store(manager, _shared_target()).save_credentials("spotify", _LEGACY_DOCUMENT)
    context = _context(paths, manager, _shared_target())

    credentials = load_oauth_credentials_snapshot_sync(context).credentials

    assert credentials == {key: value for key, value in _ADOPTED.items() if key != "client_id"}
    assert not oauth_credentials_usable(context.provider, paths, credentials)


def test_the_agent_keys_page_shows_a_legacy_connection_as_connected(tmp_path: Path) -> None:
    """The egress pages read a scope with old tokens as connected, as the dashboard does."""
    paths, manager = _runtime(tmp_path)
    _legacy_store(manager, _shared_target()).save_credentials("spotify", _LEGACY_DOCUMENT)

    connected, _label = oauth_connection_for_display(_context(paths, manager, _shared_target()))

    assert connected
