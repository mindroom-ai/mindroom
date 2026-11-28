"""Serve an auxiliary HTTP app beside the primary API server."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Literal

import uvicorn
from uvicorn.protocols.http.auto import AutoHTTPProtocol

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from starlette.types import ASGIApp

    from mindroom.constants import RuntimePaths

__all__ = ["SCRIPT_GATEWAY_PORT_ENV", "listener_port", "serve_listener"]

logger = get_logger(__name__)

SCRIPT_GATEWAY_PORT_ENV = "MINDROOM_SCRIPT_GATEWAY_PORT"
# Matches the primary API's shutdown grace; Uvicorn cancels listener requests still open after it.
_LISTENER_SHUTDOWN_GRACE_SECONDS = 5


class _ListenerServer(uvicorn.Server):
    """Uvicorn server that leaves process signals to the primary API server it runs beside."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.stop_requested = False

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Install no handlers; the primary server owns shutdown signals."""
        yield

    async def on_tick(self, counter: int) -> bool:
        """End the main loop once the owner leaves, always through Uvicorn's shutdown.

        Uvicorn skips its shutdown, leaving the listener serving, when `should_exit` is already set as startup ends.
        """
        return self.stop_requested or await super().on_tick(counter)


class _UnloggedHTTPProtocol(AutoHTTPProtocol):  # ty: ignore[unsupported-base] - Uvicorn's HTTP protocol for this install
    """Uvicorn's HTTP protocol, writing no access log for its listener alone.

    Uvicorn's own ``access_log=False`` empties the process-wide access logger, so the API's access log would stop too.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.access_log = False


def listener_port(runtime_paths: RuntimePaths, env_name: str) -> int | None:
    """Return the TCP port ``env_name`` sets, None while it is unset, or raise ``ValueError`` for any other value."""
    raw_port = (runtime_paths.env_value(env_name) or "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        msg = f"{env_name} must be a TCP port from 1 to 65535."
        raise ValueError(msg)
    return port


@asynccontextmanager
async def serve_listener(
    app: ASGIApp,
    *,
    name: str,
    host: str,
    port: int,
    log_level: str,
    ws: Literal["none", "websockets-sansio"],
    access_log: bool,
) -> AsyncIterator[None]:
    """Serve ``app`` on ``port`` beside the primary API server, as the listener ``name``, until the caller leaves."""
    server = _ListenerServer(
        uvicorn.Config(
            app,
            lifespan="off",
            log_level=log_level.lower(),
            http="auto" if access_log else _UnloggedHTTPProtocol,
            ws=ws,
            timeout_graceful_shutdown=_LISTENER_SHUTDOWN_GRACE_SECONDS,
        ),
    )
    listener = socket.create_server((host, port), family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    serve_task = asyncio.create_task(server.serve(sockets=[listener]), name=name)
    logger.info(f"{name}_started", host=host, port=port)
    try:
        yield
    finally:
        server.stop_requested = True
        # Uvicorn's shutdown closes the listener, then cancels requests still open after the grace period,
        # so owner cancellation must not interrupt it.
        await run_coroutine_until_complete(asyncio.wait({serve_task}))
    serve_task.result()
