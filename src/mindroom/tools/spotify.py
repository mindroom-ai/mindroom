"""Spotify tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.spotify import SpotifyTools


@register_tool_with_metadata(
    name="spotify",
    file_access=ToolFileAccess.NONE,
    display_name="Spotify",
    description="Search tracks, manage playlists, get recommendations, and control playback on Spotify",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    auth_provider="spotify",
    icon="SiSpotify",
    icon_color="text-green-500",
    # Token refresh needs the OAuth client secret, which only the primary process holds.
    requires_primary_runtime=True,
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    config_fields=[
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
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#spotify",
    function_names=("spotify_search", "spotify_library", "spotify_playlist_write", "spotify_play"),
    action_scoped_grants=("spotify_search", "spotify_library", "spotify_playlist_write"),
)
def spotify_tools() -> type[SpotifyTools]:
    """Return Spotify tools for music search and playlist management."""
    from mindroom.custom_tools.spotify import SpotifyTools

    return SpotifyTools
