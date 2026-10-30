"""Built-in Spotify OAuth provider."""

from __future__ import annotations

from mindroom.oauth.legacy_spotify_credentials import LEGACY_SPOTIFY_CREDENTIALS
from mindroom.oauth.providers import OAuthProvider

__all__ = ["SPOTIFY_SCOPES", "spotify_oauth_provider"]

# What the Spotify toolkit reads and changes: the profile, playback, top items, and playlists.
SPOTIFY_SCOPES = (
    "user-read-private",
    "user-read-email",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-top-read",
    "playlist-read-private",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-modify-playback-state",
)


def spotify_oauth_provider() -> OAuthProvider:
    """Return the built-in Spotify OAuth provider definition."""
    return OAuthProvider(
        id="spotify",
        display_name="Spotify",
        authorization_url="https://accounts.spotify.com/authorize",
        token_url="https://accounts.spotify.com/api/token",  # noqa: S106
        scopes=SPOTIFY_SCOPES,
        credential_service="spotify_oauth",
        tool_config_service="spotify",
        client_config_services=("spotify_oauth_client",),
        default_redirect_path="/api/oauth/spotify/callback",
        # Spotify's token endpoint authenticates the client with HTTP Basic credentials.
        token_endpoint_auth_method="client_secret_basic",  # noqa: S106
        legacy_credentials=LEGACY_SPOTIFY_CREDENTIALS,
    )
