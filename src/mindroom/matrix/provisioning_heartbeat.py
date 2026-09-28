"""Best-effort liveness reports from a paired install to the hosted provisioning service.

MindRoom Chat lists each paired install with the last time the provisioning service heard from it.
A heartbeat carries only the paired client credentials, never messages, configuration, or other content.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import runtime_matrix_ssl_verify
from mindroom.http_error_detail import error_detail_from_response
from mindroom.logging_config import get_logger
from mindroom.matrix.provisioning import CONNECTION_REVOKED_DETAIL
from mindroom.matrix.provisioning_env import (
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
_REVOKED_WARNING = "This install's connection was revoked in MindRoom Chat; run `mindroom connect` to pair again."


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


async def _send_heartbeat(url: str, client_id: str, client_secret: str, runtime_paths: RuntimePaths) -> bool:
    """Send one heartbeat and return whether later heartbeats are still worth sending."""
    headers = {
        "X-Local-MindRoom-Client-Id": client_id,
        "X-Local-MindRoom-Client-Secret": client_secret,
    }
    try:
        async with httpx.AsyncClient(
            timeout=_HEARTBEAT_TIMEOUT_SECONDS,
            verify=runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
        ) as client:
            response = await client.post(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is not an HTTPError; a malformed MINDROOM_PROVISIONING_URL must not end the task with an exception.
        logger.debug("Provisioning heartbeat failed", error=str(exc))
        return True

    if response.status_code == 403 and error_detail_from_response(response) == CONNECTION_REVOKED_DETAIL:
        logger.warning(_REVOKED_WARNING)
        return False
    # 404 means an older provisioning service without the heartbeat endpoint.
    if not response.is_success and response.status_code != 404:
        logger.debug("Provisioning heartbeat rejected", status_code=response.status_code)
    return True


async def run_provisioning_heartbeat(
    runtime_paths: RuntimePaths,
    *,
    interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Report this paired install as running at startup and then every interval until cancelled or revoked."""
    target = _heartbeat_target(runtime_paths)
    if target is None:
        return
    url, client_id, client_secret = target
    while await _send_heartbeat(url, client_id, client_secret, runtime_paths):
        await sleep(interval_seconds)
