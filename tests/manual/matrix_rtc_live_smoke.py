#!/usr/bin/env python3
"""Live smoke test for the MatrixRTC voice-call backend.

Exercises the deployed Element Call stack (LiveKit SFU + lk-jwt-service)
against a real homeserver, using the same helpers the MindRoom CallManager
uses. It proves the control-plane + credential chain end to end without
needing a second Element Call client:

1. Token-register a throwaway Matrix user on the homeserver.
2. Discover the LiveKit service URL from ``.well-known`` ``rtc_foci``.
3. Request a Matrix OpenID token.
4. Exchange it at lk-jwt-service for a real LiveKit JWT (``request_sfu_grant``).
5. Connect to the LiveKit SFU signaling endpoint with that JWT.

This uses real network services, so it is not part of normal pytest. Run it
manually against a deployment:

    REG_TOKEN=<matrix-registration-token> \
    MATRIX_HOMESERVER=https://mindroom.chat \
    uv run python tests/manual/matrix_rtc_live_smoke.py

The Matrix requests use httpx directly (not nio) so the script works from
environments where aiohttp lacks a CA bundle. Media/ICE is not asserted:
signaling reaching CONNECTED is the deployment health signal. A throwaway
account is created and left inert (registration cannot self-deactivate
without a full UIA password flow).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys

import httpx

from mindroom.matrix_rtc.focus import (
    OpenIDToken,
    discover_livekit_service_url,
    request_sfu_grant,
)

HOMESERVER = os.environ.get("MATRIX_HOMESERVER", "https://mindroom.chat")
SFU_ROOM = "!rtc-live-smoke:example"


def log(msg: str) -> None:
    """Print one status line and flush immediately."""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


async def _register(http: httpx.AsyncClient, username: str, password: str, reg_token: str) -> tuple[str, str, str]:
    start = await http.post("/_matrix/client/v3/register", json={"username": username, "password": password})
    session = start.json()["session"]
    resp = await http.post(
        "/_matrix/client/v3/register",
        json={
            "username": username,
            "password": password,
            "auth": {"type": "m.login.registration_token", "token": reg_token, "session": session},
        },
    )
    body = resp.json()
    if "access_token" not in body:
        msg = f"registration did not complete (status={resp.status_code}, body={body})"
        raise RuntimeError(msg)
    return body["user_id"], body["device_id"], body["access_token"]


async def _openid(http: httpx.AsyncClient, user_id: str, access_token: str) -> OpenIDToken:
    resp = await http.post(
        f"/_matrix/client/v3/user/{user_id}/openid/request_token",
        params={"access_token": access_token},
        json={},
    )
    resp.raise_for_status()
    body = resp.json()
    return OpenIDToken(
        access_token=body["access_token"],
        expires_in=body["expires_in"],
        matrix_server_name=body["matrix_server_name"],
        token_type=body["token_type"],
    )


async def main() -> int:
    """Run the live smoke stages, printing PASS/FAIL for each."""
    reg_token = os.environ["REG_TOKEN"].strip()
    suffix = secrets.token_hex(4)
    username = f"rtc_live_smoke_{suffix}"
    password = secrets.token_urlsafe(20)

    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as http:
        user_id, device_id, access_token = await _register(http, username, password, reg_token)
        log(f"PASS register: {user_id} device={device_id}")

        service_url = await discover_livekit_service_url(HOMESERVER)
        log(f"{'PASS' if service_url else 'FAIL'} well-known rtc_foci: {service_url}")
        if not service_url:
            return 1

        openid = await _openid(http, user_id, access_token)
        log(f"PASS openid: server_name={openid.matrix_server_name}")

        grant = await request_sfu_grant(
            service_url,
            room_id=SFU_ROOM,
            device_id=device_id,
            openid_token=openid,
        )
        log(f"PASS sfu grant: url={grant.url} jwt_len={len(grant.jwt)}")

    from livekit import rtc  # noqa: PLC0415

    room = rtc.Room()
    try:
        await asyncio.wait_for(room.connect(grant.url, grant.jwt), timeout=25)
    except Exception as error:
        log(f"FAIL sfu connect: {type(error).__name__}: {error}")
        return 1
    log(f"PASS sfu connect: identity={room.local_participant.identity} state={room.connection_state}")
    await room.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
