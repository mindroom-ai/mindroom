"""Spotify tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.spotify import SpotifyTools


@register_tool_with_metadata(
    name="spotify",
    display_name="Spotify",
    description="Search tracks, manage playlists, get recommendations, and control playback on Spotify",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiSpotify",
    icon_color="text-green-500",
    config_fields=[
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=True,
            placeholder="Spotify OAuth access token",
            description="OAuth access token with required scopes (user-read-private, playlist-modify-public, playlist-modify-private)",
        ),
        ConfigField(
            name="default_market",
            label="Default Market",
            type="text",
            required=False,
            default="US",
            placeholder="e.g., US, GB, DE",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/others/spotify",
    helper_text="Get an access token from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)",
)
def spotify_tools() -> type[SpotifyTools]:
    """Return Spotify tools for music search and playlist management."""
    from agno.tools.spotify import SpotifyTools

    return SpotifyTools
