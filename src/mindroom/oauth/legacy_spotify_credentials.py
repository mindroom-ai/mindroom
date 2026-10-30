"""Spotify tokens the dashboard's own Spotify routes stored before Spotify became an OAuth provider."""

# LEGACY_COMPAT: Spotify tokens saved by the former `/api/integrations/spotify` routes.
# Legacy format: a `spotify` credential document holding `access_token`, `refresh_token`, `expires_at`, `username`, and `_source: "ui"` in the store of the unscoped or `shared` scope the dashboard selected (Spotify only ever ran on those); tests/fixtures/legacy_spotify_credentials.json records one.
# Last legacy release: v2026.10.709, whose Spotify routes wrote it; replacement: the next release connects Spotify through the `spotify` OAuth provider, which stores tokens under `spotify_oauth` and the client under `spotify_oauth_client`.
# Handling: a scope's `spotify_oauth` store starts with the converted tokens when it is created, read from the exact store the old routes wrote; the issuing client is recorded from `SPOTIFY_CLIENT_ID`, the client the old routes renewed those tokens with, read here only and only while it is still set.
# After the new store commits, the token fields leave the `spotify` document, which keeps any tool settings, so the conversion runs once per scope; a removal that fails leaves a copy nothing reads.
# A connection whose client is unknown, or differs from the configured `spotify_oauth_client`, reads as not connected and needs a reconnect.
# Coverage: tests/test_spotify_oauth.py::test_a_legacy_connection_carries_over_into_the_oauth_store,
# tests/test_spotify_oauth.py::test_the_conversion_keeps_the_tool_settings_and_runs_once,
# tests/test_spotify_oauth.py::test_a_listing_shows_a_legacy_connection_as_connected,
# tests/test_spotify_oauth.py::test_a_legacy_connection_without_a_known_client_needs_a_reconnect.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from mindroom.logging_config import get_logger
from mindroom.oauth.providers import OAuthLegacyCredentials

if TYPE_CHECKING:
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.oauth.credential_store_types import OAuthCredentialStoreContext

logger = get_logger(__name__)

_SERVICE = "spotify"
_TOKEN_FIELDS = frozenset({"access_token", "refresh_token", "expires_at", "username"})


def _legacy_store(context: OAuthCredentialStoreContext) -> CredentialsManager | None:
    """Return the store the old routes wrote this scope's tokens to: the primary's own, or the agent's shared one."""
    target = context.worker_target
    if target is None or target.worker_scope is None:
        return context.credentials_manager
    if target.worker_scope != "shared" or not target.routing_agent_name:
        # Spotify only ever ran unscoped or on the shared scope.
        return None
    return context.credentials_manager.for_primary_runtime_agent_scope(target.routing_agent_name)


def _legacy_document(store: CredentialsManager | None) -> dict[str, Any] | None:
    if store is None:
        return None
    try:
        return store.load_credentials(_SERVICE)
    except (OSError, TypeError, ValueError, InvalidTag) as exc:
        # An unreadable old document must not keep the scope from connecting again.
        logger.warning("Legacy Spotify credentials could not be read", error_type=type(exc).__name__)
        return None


def _read(context: OAuthCredentialStoreContext) -> dict[str, Any] | None:
    """Return the scope's legacy tokens in the OAuth store's format, or None when it holds none."""
    document = _legacy_document(_legacy_store(context)) or {}
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    credentials: dict[str, Any] = {
        "token": access_token,
        "scopes": list(context.provider.scopes),
        "token_uri": context.provider.token_url,
        "_source": "oauth",
        "_oauth_provider": context.provider.id,
    }
    refresh_token = document.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        credentials["refresh_token"] = refresh_token
    expires_at = document.get("expires_at")
    if isinstance(expires_at, int | float) and not isinstance(expires_at, bool):
        credentials["expires_at"] = expires_at
    client_id = context.runtime_paths.env_value("SPOTIFY_CLIENT_ID")
    if client_id:
        credentials["client_id"] = client_id
    return credentials


def _remove(context: OAuthCredentialStoreContext) -> None:
    """Drop the converted token fields from the scope's `spotify` document, keeping its tool settings."""
    store = _legacy_store(context)
    document = _legacy_document(store)
    if store is None or document is None:
        return
    settings = {key: value for key, value in document.items() if key not in _TOKEN_FIELDS}
    try:
        if settings.keys() - {"_source"}:
            store.save_credentials(_SERVICE, settings)
        else:
            store.delete_credentials(_SERVICE)
    except OSError as exc:
        logger.warning("Converted legacy Spotify tokens could not be removed", error_type=type(exc).__name__)


LEGACY_SPOTIFY_CREDENTIALS = OAuthLegacyCredentials(read=_read, remove=_remove)
