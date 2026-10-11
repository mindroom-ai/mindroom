"""Renewal of the Spotify OAuth access token saved by the dashboard connect flow."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"  # noqa: S105
_RENEWAL_MARGIN_SECONDS = 60


def current_spotify_credentials(
    credentials: dict[str, Any],
    runtime_paths: RuntimePaths,
    save_credentials: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Return stored Spotify credentials, renewing and saving an access token that expires within a minute.

    Renewal needs the stored refresh token plus this process's SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.
    Without them, or when Spotify refuses the renewal, the stored credentials are returned unchanged.
    """
    refresh_token = credentials.get("refresh_token")
    expires_at = credentials.get("expires_at")
    client_id = runtime_paths.env_value("SPOTIFY_CLIENT_ID")
    client_secret = runtime_paths.env_value("SPOTIFY_CLIENT_SECRET")
    if (
        not refresh_token
        or not client_id
        or not client_secret
        or not isinstance(expires_at, int | float)
        or expires_at > time.time() + _RENEWAL_MARGIN_SECONDS
    ):
        return credentials
    response = httpx.post(
        _SPOTIFY_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        timeout=30.0,
    )
    if response.status_code != 200:
        logger.warning("Spotify refused to renew the access token", status_code=response.status_code)
        return credentials
    token = response.json()
    renewed = {
        **credentials,
        "access_token": token["access_token"],
        "expires_at": int(time.time()) + token["expires_in"],
        "refresh_token": token.get("refresh_token") or refresh_token,
    }
    save_credentials(renewed)
    return renewed
