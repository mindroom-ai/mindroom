"""Backend-only authenticated HTTP and binary websocket worker transport."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

import aiohttp
from pydantic import TypeAdapter, ValidationError

from mindroom.worker_computer.protocol import ComputerStatus
from mindroom.worker_computer.sessions import ComputerError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.workers.models import WorkerHandle

_STATUS = TypeAdapter(ComputerStatus)


def _worker_url(handle: WorkerHandle) -> str:
    return (
        handle.debug_metadata.get("api_root", handle.endpoint.removesuffix("/execute"))
        .rstrip("/")
        .removesuffix("/api/sandbox-runner")
        + "/computer"
    )


def _headers(handle: WorkerHandle) -> dict[str, str]:
    if not handle.auth_token:
        raise ComputerError(503, "Computer worker authentication is unavailable.")
    return {"X-Mindroom-Sandbox-Token": handle.auth_token}


async def computer_request(
    handle: WorkerHandle,
    operation: Literal["status", "start", "take", "release", "stop"],
    session_id: str,
    *,
    generation: str | None = None,
) -> ComputerStatus:
    """Send bounded requests with secrets in headers and sanitized failures."""
    suffix = "" if operation == "status" else "/start" if operation == "start" else "/control"
    body = {"session_id": session_id, "action": operation, "generation": generation} if suffix == "/control" else None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.request(
                "GET" if operation == "status" else "POST",
                _worker_url(handle) + suffix,
                headers=_headers(handle),
                json=body,
                allow_redirects=False,
            ) as response:
                if response.status == 409:
                    raise ComputerError(409, "Computer control is unavailable or already held by another viewer.")
                if response.status != 200:
                    raise ComputerError(503, "Computer worker is unavailable.")
                data = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ComputerError(503, "Invalid computer worker response.")
                return _STATUS.validate_python(json.loads(data))
    except (aiohttp.ClientError, TimeoutError, ValueError, ValidationError):
        raise ComputerError(503, "Computer worker is unavailable.") from None


@asynccontextmanager
async def computer_stream(
    handle: WorkerHandle,
    session_id: str,
    generation: str,
) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    """Connect only to a manager-issued worker; URL carries no credential."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as client:  # noqa: SIM117 - own the client until response cleanup completes
            async with client.ws_connect(
                _worker_url(handle) + "/stream",
                headers=_headers(handle),
                params={"session_id": session_id, "generation": generation},
                protocols=("binary",),
                max_msg_size=4 * 1024 * 1024,
                autoclose=True,
            ) as websocket:
                yield websocket
    except (aiohttp.ClientError, TimeoutError):
        raise ComputerError(503, "Computer stream is unavailable.") from None
