"""Best-effort liveness reports from a paired install to the hosted provisioning service.

MindRoom Chat lists each paired install with the last time the provisioning service heard from it.
A heartbeat carries only the paired client credentials, never messages, configuration, or other content.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from mindroom.logging_config import get_logger
from mindroom.matrix.provisioning import local_client_credentials_rejected
from mindroom.matrix.provisioning_env import (
    local_client_headers,
    local_provisioning_client_credentials_from_env,
    provisioning_url_from_env,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 6 * 60 * 60
_HEARTBEAT_TIMEOUT_SECONDS = 10.0
_HEARTBEAT_PATH = "/v1/local-mindroom/heartbeat"
_REJECTED_WARNING = (
    "The provisioning service rejected this install's credentials as invalid or revoked; "
    "run `mindroom connect` to pair again."
)


def _heartbeat_target(runtime_paths: RuntimePaths) -> tuple[str, str, str] | None:
    """Return the heartbeat URL and client credentials, or None for installs that are not paired."""
    provisioning_url = provisioning_url_from_env(runtime_paths)
    if provisioning_url is None:
        return None
    try:
        credentials = local_provisioning_client_credentials_from_env(runtime_paths)
    except ValueError:
        # Incomplete credentials are reported by the startup paths that actually need them.
        return None
    if credentials is None:
        return None
    client_id, client_secret = credentials
    return f"{provisioning_url}{_HEARTBEAT_PATH}", client_id, client_secret


async def _send_heartbeat(url: str, client_id: str, client_secret: str) -> bool:
    """Send one heartbeat and return whether later heartbeats are still worth sending."""
    try:
        # The request carries the client secret, so TLS is verified whatever MATRIX_SSL_VERIFY says.
        async with httpx.AsyncClient(timeout=_HEARTBEAT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=local_client_headers(client_id, client_secret))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is not an HTTPError; a malformed MINDROOM_PROVISIONING_URL must not end the task with an exception.
        logger.debug("Provisioning heartbeat failed", error=str(exc))
        return True

    if local_client_credentials_rejected(response):
        logger.warning(_REJECTED_WARNING)
        return False
    # 404 means an older provisioning service without the heartbeat endpoint; httpx still logs its usual request line.
    if not response.is_success and response.status_code != 404:
        logger.debug("Provisioning heartbeat rejected", status_code=response.status_code)
    return True


async def run_provisioning_heartbeat(
    runtime_paths: RuntimePaths,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Report this paired install as running at startup and then every six hours until cancelled or rejected."""
    target = _heartbeat_target(runtime_paths)
    if target is None:
        return
    url, client_id, client_secret = target
    while await _send_heartbeat(url, client_id, client_secret):
        await sleep(_HEARTBEAT_INTERVAL_SECONDS)
