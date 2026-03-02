"""Third-party service integrations API."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.credentials import CredentialsManager
from mindroom.tool_dependencies import ensure_tool_deps
from mindroom.tools_metadata import ensure_tool_registry_loaded, export_tools_metadata

if TYPE_CHECKING:
    from spotipy import Spotify, SpotifyOAuth

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Initialize credentials manager
creds_manager = CredentialsManager()


def _ensure_spotify_packages() -> tuple[type[Spotify], type[SpotifyOAuth]]:
    """Lazily import Spotify packages, auto-installing if needed."""
    ensure_tool_deps(["spotipy"], "spotify")

    from spotipy import Spotify as _Spotify  # noqa: PLC0415
    from spotipy import SpotifyOAuth as _SpotifyOAuth  # noqa: PLC0415

    return _Spotify, _SpotifyOAuth


# Load tool metadata from the single source of truth
def _get_tools_metadata() -> dict[str, Any]:
    """Load tool metadata from the in-memory registry."""
    from mindroom.api.main import load_runtime_config  # noqa: PLC0415

    config, config_path = load_runtime_config()
    ensure_tool_registry_loaded(config, config_path=config_path)
    tools = export_tools_metadata()
    return {tool["name"]: tool for tool in tools}


class ServiceStatus(BaseModel):
    """Service connection status."""

    service: str
    connected: bool
    display_name: str
    icon: str
    category: str
    requires_oauth: bool
    requires_api_key: bool
    details: dict[str, Any] | None = None
    error: str | None = None


class _ApiKeyRequest(BaseModel):
    """API key configuration request."""

    service: str
    api_key: str
    api_secret: str | None = None


def _get_service_credentials(service: str) -> dict[str, Any]:
    """Get stored credentials for a service."""
    credentials = creds_manager.load_credentials(service)
    return credentials if credentials else {}


def _save_service_credentials(service: str, credentials: dict[str, Any]) -> None:
    """Save service credentials."""
    creds_manager.save_credentials(service, credentials)


@router.get("/{service}/status")
async def get_service_status(service: str) -> ServiceStatus:
    """Get connection status for a specific service."""
    # Get tool metadata from single source of truth
    tools_metadata = _get_tools_metadata()

    if service not in tools_metadata:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    tool = tools_metadata[service]
    status = ServiceStatus(
        service=service,
        connected=False,
        display_name=tool.get("display_name", service),
        icon=tool.get("icon", "📦"),
        category=tool.get("category", "other"),
        requires_oauth=tool.get("setup_type") == "oauth",
        requires_api_key=tool.get("setup_type") == "api_key",
    )

    creds = _get_service_credentials(service)
    if creds:
        if service == "spotify":
            status.connected = "access_token" in creds
            if status.connected:
                try:
                    # Try to get user info
                    spotify_cls, _ = _ensure_spotify_packages()
                    sp = spotify_cls(auth=creds["access_token"])
                    user = sp.current_user()
                    status.details = {
                        "username": user["display_name"],
                        "email": user.get("email"),
                        "product": user.get("product"),
                    }
                except Exception as e:
                    status.connected = False
                    status.error = str(e)
        else:
            status.connected = "api_key" in creds

    return status


# Spotify
@router.post("/spotify/connect")
async def connect_spotify() -> dict[str, str]:
    """Start Spotify OAuth flow."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Spotify OAuth not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.",
        )

    _, spotify_oauth_cls = _ensure_spotify_packages()
    sp_oauth = spotify_oauth_cls(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8000/api/integrations/spotify/callback",
        scope="user-read-private user-read-email user-read-playback-state user-read-currently-playing user-top-read",
    )

    auth_url = sp_oauth.get_authorize_url()
    return {"auth_url": auth_url}


@router.get("/spotify/callback")
async def spotify_callback(code: str) -> RedirectResponse:
    """Handle Spotify OAuth callback."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Spotify OAuth not configured")

    try:
        spotify_cls, spotify_oauth_cls = _ensure_spotify_packages()
        sp_oauth = spotify_oauth_cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://localhost:8000/api/integrations/spotify/callback",
        )

        token_info = sp_oauth.get_access_token(code)

        # Get user info
        sp = spotify_cls(auth=token_info["access_token"])
        user = sp.current_user()

        # Save credentials
        credentials = {
            "access_token": token_info["access_token"],
            "refresh_token": token_info.get("refresh_token"),
            "expires_at": token_info.get("expires_at"),
            "username": user["display_name"],
        }
        _save_service_credentials("spotify", credentials)

        # Redirect back to widget
        return RedirectResponse(url="http://localhost:5173/?spotify=connected")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth failed: {e!s}") from e


@router.post("/{service}/disconnect")
async def disconnect_service(service: str) -> dict[str, str]:
    """Disconnect a service by removing stored credentials."""
    # Get tool metadata from single source of truth
    tools_metadata = _get_tools_metadata()

    if service not in tools_metadata:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")

    # Delete credentials using the manager
    creds_manager.delete_credentials(service)

    return {"status": "disconnected"}
