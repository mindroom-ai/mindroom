"""LiveKit focus discovery and SFU credential exchange for MatrixRTC calls.

The MatrixRTC Authorization Service (``lk-jwt-service``) verifies a Matrix
OpenID token and mints a LiveKit JWT scoped to the call's SFU room. The
service URL is advertised on the Matrix server name's
``.well-known/matrix/client`` under ``org.matrix.msc4143.rtc_foci``.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import ServerFetchAsyncHTTPTransport

logger = get_logger(__name__)

_RTC_FOCI_WELL_KNOWN_KEY = "org.matrix.msc4143.rtc_foci"


@dataclass(frozen=True)
class OpenIDToken:
    """A Matrix OpenID token as returned by the CS API."""

    access_token: str
    expires_in: int
    matrix_server_name: str
    token_type: str


@dataclass(frozen=True)
class SfuGrant:
    """Connection credentials for one LiveKit SFU room."""

    url: str
    jwt: str


async def discover_livekit_service_url(
    server_name: str,
    *,
    ssl_verify: bool = True,
    allow_private_networks: bool = False,
) -> str | None:
    """Read the LiveKit service URL from a Matrix server name's client well-known."""
    well_known_url = f"https://{server_name}/.well-known/matrix/client"
    transport = ServerFetchAsyncHTTPTransport(
        allow_private_networks=allow_private_networks,
        verify=ssl_verify,
    )
    async with httpx.AsyncClient(transport=transport, timeout=10.0, follow_redirects=True) as client:
        try:
            response = await client.get(well_known_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("rtc_well_known_fetch_failed", url=well_known_url, error=str(error))
            return None
    if not isinstance(payload, dict):
        logger.warning("rtc_well_known_not_a_dict", url=well_known_url, payload_type=type(payload).__name__)
        return None
    foci = payload.get(_RTC_FOCI_WELL_KNOWN_KEY)
    if not isinstance(foci, list):
        return None
    for focus in foci:
        if isinstance(focus, dict) and focus.get("type") == "livekit":
            url = focus.get("livekit_service_url")
            if isinstance(url, str) and url:
                return url
    return None


async def request_sfu_grant(
    livekit_service_url: str,
    *,
    room_id: str,
    device_id: str,
    openid_token: OpenIDToken,
    ssl_verify: bool = True,
    allow_private_networks: bool = False,
) -> SfuGrant:
    """Exchange a Matrix OpenID token for LiveKit SFU credentials.

    Uses the ``/sfu/get`` endpoint of the MatrixRTC Authorization Service.
    The service derives the SFU room from the Matrix room ID and the LiveKit
    participant identity from the verified Matrix user ID plus ``device_id``.
    """
    endpoint = f"{livekit_service_url.rstrip('/')}/sfu/get"
    request_body = {
        "room": room_id,
        "openid_token": {
            "access_token": openid_token.access_token,
            "token_type": openid_token.token_type,
            "matrix_server_name": openid_token.matrix_server_name,
            "expires_in": openid_token.expires_in,
        },
        "device_id": device_id,
    }
    transport = ServerFetchAsyncHTTPTransport(
        allow_private_networks=allow_private_networks,
        verify=ssl_verify,
    )
    async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
        response = await client.post(endpoint, json=request_body)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        msg = f"MatrixRTC authorization service returned a non-object grant: {type(payload).__name__}"
        # ValueError (not TypeError) is the invalid-grant contract callers catch.
        raise ValueError(msg)  # noqa: TRY004
    url = payload.get("url")
    jwt = payload.get("jwt")
    if not isinstance(url, str) or not url or not isinstance(jwt, str) or not jwt:
        msg = f"MatrixRTC authorization service returned an invalid grant: {sorted(payload)}"
        raise ValueError(msg)
    return SfuGrant(url=url, jwt=jwt)
