"""The tailnet SSH gateway's listener: three routes on a port of their own, for the gateway alone.

With ``MINDROOM_TAILNET_SSH_GATEWAY_TOKEN`` set, the primary serves on ``MINDROOM_TAILNET_SSH_PORT`` what the gateway
is to run (``GET /links``), takes what it sees (``POST /status``), and tells it where each connection it accepted goes
(``POST /links/{id}/connect``). The listener's app holds nothing else, so a network path that reaches only this port
reaches no other primary API route, and every request must carry the token.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.script_gateway import SCRIPT_GATEWAY_PORT_ENV, listener_port, serve_listener
from mindroom.tailnet_ssh.links import (
    ConnectRefused,
    GatewayStatus,
    active_worker_keys,
    connect,
    desired_links,
    record_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import ASGIApp, Receive, Scope, Send

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = [
    "DEFAULT_PORT",
    "PORT_ENV",
    "TOKEN_ENV",
    "active_tailnet_ssh_worker_keys",
    "gateway_app",
    "serve_tailnet_ssh_gateway_listener",
    "tailnet_ssh_status",
]

TOKEN_ENV = "MINDROOM_TAILNET_SSH_GATEWAY_TOKEN"  # noqa: S105 - the variable's name
PORT_ENV = "MINDROOM_TAILNET_SSH_PORT"
DEFAULT_PORT = 8769


class _LinkReport(BaseModel):
    """What the gateway sees of one link's node."""

    state: Literal["needs_login", "needs_approval", "running", "removed"]
    login_url: str | None = None
    tailnet: str | None = None
    dns_name: str | None = None
    login_name: str | None = None
    key_expiry: str | None = None
    open_connections: int = 0
    last_error: str | None = None


class _StatusReport(BaseModel):
    """The gateway's ``POST /status`` body: how its nodes enrol, and every node it holds by link id."""

    enrollment: Literal["login", "auth_key"]
    links: dict[str, _LinkReport]


class _Peer(BaseModel):
    """The tailnet peer whose connection the gateway accepted."""

    peer_login: str
    peer_node: str


class _TokenGate:
    """Answer 401 to every request without the gateway's token, before a route is matched or a body read.

    So a caller without the token cannot tell a route from an unknown path, nor a loaded config from a broken one.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.authorization = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not secrets.compare_digest(
            dict(scope["headers"]).get(b"authorization", b""),
            self.authorization,
        ):
            await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def tailnet_ssh_status(app: FastAPI) -> GatewayStatus | None:
    """Return what the gateway last reported to the primary serving ``app``, or None while tailnet SSH is off."""
    return config_lifecycle.app_state(app).tailnet_ssh_status


def active_tailnet_ssh_worker_keys(app: FastAPI) -> frozenset[str]:
    """Return the workers with an open SSH connection, read on the app's event loop; none while tailnet SSH is off."""
    status = tailnet_ssh_status(app)
    return frozenset() if status is None else active_worker_keys(status, monotonic=time.monotonic())


def _link_service(api_app: FastAPI) -> tuple[Config, RuntimePaths, AgentReplyMembershipIndex, GatewayStatus]:
    """Return what every link service call takes, from the primary's committed config, or answer 503 without one."""
    state = config_lifecycle.app_state(api_app)
    status = state.tailnet_ssh_status
    if status is None:
        raise HTTPException(status_code=503, detail="Tailnet SSH is off.")
    try:
        config, runtime_paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    except (HTTPException, TypeError):
        # Why the config does not load is the operator's to read, not the gateway's.
        raise HTTPException(status_code=503, detail="MindRoom's configuration is not loaded.") from None
    return config, runtime_paths, state.runtime.agent_reply_memberships, status


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    """Return the request's JSON body as ``model``, or answer 422 with one sentence.

    Pydantic's errors repeat what they reject, and a status report carries login URLs, so none of them is answered.
    """
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        detail = "The request body is not what the gateway contract describes."
        raise HTTPException(status_code=422, detail=detail) from None


def gateway_app(api_app: FastAPI, token: str) -> FastAPI:
    """Return the app the gateway alone talks to: the link service of the primary serving ``api_app``.

    Each route answers 503 for a primary that cannot serve it before it reads the request's body, which is why the
    routes read their bodies themselves: FastAPI would parse a declared body, and answer for it, first.
    """
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(_TokenGate, token=token)

    @app.get("/links")
    async def links() -> list[dict[str, object]]:
        config, runtime_paths, memberships, status = _link_service(api_app)
        return await asyncio.to_thread(desired_links, config, runtime_paths, memberships, status, now=time.time())

    @app.post("/status", status_code=204)
    async def post_status(request: Request) -> None:
        _config, runtime_paths, _memberships, status = _link_service(api_app)
        report = await _body(request, _StatusReport)
        await asyncio.to_thread(
            record_status,
            runtime_paths,
            status,
            report.model_dump(),
            now=time.time(),
            monotonic=time.monotonic(),
        )

    @app.post("/links/{link_id}/connect")
    async def connect_link(link_id: str, request: Request) -> dict[str, str | int]:
        config, runtime_paths, memberships, status = _link_service(api_app)
        peer = await _body(request, _Peer)
        try:
            host, port = await connect(
                config,
                runtime_paths,
                memberships,
                status,
                link_id,
                peer.peer_login,
                peer.peer_node,
            )
        except ConnectRefused as refused:
            raise HTTPException(status_code=refused.status_code, detail=refused.detail) from None
        return {"host": host, "port": port}

    return app


@asynccontextmanager
async def serve_tailnet_ssh_gateway_listener(
    runtime_paths: RuntimePaths,
    *,
    host: str,
    api_port: int,
    api_app: FastAPI,
    log_level: str,
) -> AsyncIterator[None]:
    """Serve the gateway's routes on ``MINDROOM_TAILNET_SSH_PORT`` while the primary API runs.

    Nothing is served, and tailnet SSH is off, while ``MINDROOM_TAILNET_SSH_GATEWAY_TOKEN`` is unset.
    """
    token = (runtime_paths.env_value(TOKEN_ENV) or "").strip()
    if not token:
        yield
        return
    port = listener_port(runtime_paths, PORT_ENV) or DEFAULT_PORT
    if port in (api_port, listener_port(runtime_paths, SCRIPT_GATEWAY_PORT_ENV)):
        msg = f"{PORT_ENV} must differ from the API's port and from {SCRIPT_GATEWAY_PORT_ENV}, not {port}."
        raise ValueError(msg)
    state = config_lifecycle.app_state(api_app)
    state.tailnet_ssh_status = GatewayStatus()
    try:
        async with serve_listener(
            gateway_app(api_app, token),
            name="tailnet_ssh_gateway_listener",
            host=host,
            port=port,
            log_level=log_level,
            ws="none",
            # The gateway asks every few seconds; its calls would fill the access log.
            access_log=False,
        ):
            yield
    finally:
        state.tailnet_ssh_status = None
