"""Spotify integration API."""

from __future__ import annotations

import os
from typing import Any, Protocol, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mindroom.api.credentials import (
    consume_pending_oauth_state,
    issue_pending_oauth_state,
    load_credentials_for_target,
    resolve_request_credentials_target,
)
from mindroom.tool_system.dependencies import ensure_tool_deps

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def get_dashboard_url(request: Request) -> str:
    """Return the dashboard base URL for OAuth redirects."""
    return str(request.base_url).rstrip("/")


def _get_spotify_redirect_uri(request: Request) -> str:
    """Return the Spotify OAuth callback URL."""
    configured = os.getenv("SPOTIFY_REDIRECT_URI")
    if configured:
        return configured
    return str(request.url_for("spotify_callback"))


class _SpotifyClientProtocol(Protocol):
    def current_user(self) -> dict[str, Any]: ...


class _SpotifyClientFactoryProtocol(Protocol):
    def __call__(self, *, auth: str) -> _SpotifyClientProtocol: ...


class _SpotifyOAuthClientProtocol(Protocol):
    def get_authorize_url(self, state: str | None = None) -> str: ...

    def get_access_token(self, code: str) -> dict[str, Any]: ...


class _SpotifyOAuthFactoryProtocol(Protocol):
    def __call__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scope: str = "",
    ) -> _SpotifyOAuthClientProtocol: ...


def _ensure_spotify_packages() -> tuple[_SpotifyClientFactoryProtocol, _SpotifyOAuthFactoryProtocol]:
    """Lazily import Spotify packages, auto-installing if needed."""
    ensure_tool_deps(["spotipy"], "spotify")

    from spotipy import Spotify as _Spotify  # noqa: PLC0415
    from spotipy import SpotifyOAuth as _SpotifyOAuth  # noqa: PLC0415

    return (
        cast("_SpotifyClientFactoryProtocol", _Spotify),
        cast("_SpotifyOAuthFactoryProtocol", _SpotifyOAuth),
    )


class SpotifyStatus(BaseModel):
    """Spotify connection status."""

    connected: bool
    details: dict[str, Any] | None = None
    error: str | None = None


def _get_spotify_credentials(request: Request, agent_name: str | None = None) -> dict[str, Any]:
    """Get stored Spotify credentials."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("spotify",))
    credentials = load_credentials_for_target("spotify", target)
    return credentials if credentials else {}


def _save_spotify_credentials(credentials: dict[str, Any], request: Request, agent_name: str | None = None) -> None:
    """Save Spotify credentials."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("spotify",))
    target.target_manager.save_credentials("spotify", credentials)


@router.get("/spotify/status")
async def get_spotify_status(
    request: Request,
    agent_name: str | None = None,
) -> SpotifyStatus:
    """Get Spotify connection status."""
    status = SpotifyStatus(connected=False)
    creds = _get_spotify_credentials(request, agent_name)
    if not creds or "access_token" not in creds:
        return status

    status.connected = True
    try:
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

    return status


# Spotify
@router.post("/spotify/connect")
async def connect_spotify(request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Start Spotify OAuth flow."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Spotify OAuth not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.",
        )

    resolve_request_credentials_target(request, agent_name=agent_name, service_names=("spotify",))
    state = issue_pending_oauth_state(request, "spotify", agent_name)
    _, spotify_oauth_cls = _ensure_spotify_packages()
    sp_oauth = spotify_oauth_cls(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_get_spotify_redirect_uri(request),
        scope="user-read-private user-read-email user-read-playback-state user-read-currently-playing user-top-read",
    )

    auth_url = sp_oauth.get_authorize_url(state=state)
    return {"auth_url": auth_url}


@router.get("/spotify/callback")
async def spotify_callback(request: Request, code: str) -> RedirectResponse:
    """Handle Spotify OAuth callback."""
    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="No OAuth state received")

    from mindroom.api.main import verify_user  # noqa: PLC0415

    await verify_user(request, request.headers.get("authorization"), allow_public_paths=False)
    agent_name = consume_pending_oauth_state(request, "spotify", state)

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Spotify OAuth not configured")

    try:
        spotify_cls, spotify_oauth_cls = _ensure_spotify_packages()
        sp_oauth = spotify_oauth_cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=_get_spotify_redirect_uri(request),
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
        _save_spotify_credentials(credentials, request, agent_name)

        return RedirectResponse(url=f"{get_dashboard_url(request)}/?spotify=connected")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OAuth failed: {e!s}") from e


@router.post("/spotify/disconnect")
async def disconnect_spotify(request: Request, agent_name: str | None = None) -> dict[str, str]:
    """Disconnect Spotify by removing stored credentials."""
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=("spotify",))
    target.target_manager.delete_credentials("spotify")

    return {"status": "disconnected"}
