"""Three bearer-only tailnet SSH routes on a separate listener."""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.script_gateway import SCRIPT_GATEWAY_PORT_ENV, listener_port, serve_listener
from mindroom.tailnet_ssh import links as ssh

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.responses import Response

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

TOKEN_ENV = "MINDROOM_TAILNET_SSH_GATEWAY_TOKEN"  # noqa: S105 - the variable's name
PORT_ENV = "MINDROOM_TAILNET_SSH_PORT"
DEFAULT_PORT = 8769


class _Peer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    peer_login: str
    peer_node: str


def active_tailnet_ssh_worker_keys(app: FastAPI) -> frozenset[str]:
    """Return the workers held by fresh SSH activity."""
    status = config_lifecycle.app_state(app).tailnet_ssh_status
    return frozenset() if status is None else ssh.active_worker_keys(status, monotonic=time.monotonic())


def _read_config(api_app: FastAPI) -> tuple[Config, RuntimePaths]:
    try:
        return config_lifecycle.read_app_committed_runtime_config(api_app)
    except (HTTPException, TypeError):
        raise HTTPException(503, "MindRoom's configuration is not loaded.") from None


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(422, "Invalid gateway request body.") from None


def gateway_app(api_app: FastAPI, token: str) -> FastAPI:
    """Build the gateway listener; check config before parsing requested bodies."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    authorization = f"Bearer {token}".encode()

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not secrets.compare_digest(dict(request.headers.raw).get(b"authorization", b""), authorization):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    read_config = partial(_read_config, api_app)

    def ready() -> tuple[Config, RuntimePaths, ssh.GatewayStatus]:
        status = config_lifecycle.app_state(api_app).tailnet_ssh_status
        if status is None:
            raise HTTPException(503, "Tailnet SSH is disabled.")
        return (*read_config(), status)

    @app.get("/links")
    async def links() -> list[dict[str, object]]:
        config, paths, _ = ready()
        memberships = config_lifecycle.app_state(api_app).runtime.agent_reply_memberships
        return await asyncio.to_thread(ssh.desired_links, config, paths, memberships)

    @app.post("/status", status_code=204)
    async def post_status(request: Request) -> None:
        _, paths, status = ready()
        report = await _body(request, ssh.StatusReport)
        await asyncio.to_thread(ssh.record_status, paths, status, report, monotonic=time.monotonic())

    @app.post("/links/{link_id}/connect")
    async def connect_link(link_id: str, request: Request) -> dict[str, str | int]:
        ready()
        peer = await _body(request, _Peer)
        memberships = config_lifecycle.app_state(api_app).runtime.agent_reply_memberships
        host, port = await ssh.connect(read_config, memberships, link_id, peer.peer_login, peer.peer_node)
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
    """Run the separate listener only when configured with a gateway token."""
    token = (runtime_paths.env_value(TOKEN_ENV) or "").strip()
    if not token:
        yield
        return
    port = listener_port(runtime_paths, PORT_ENV) or DEFAULT_PORT
    if port in (api_port, listener_port(runtime_paths, SCRIPT_GATEWAY_PORT_ENV)):
        msg = f"{PORT_ENV} must differ from the API's port and from {SCRIPT_GATEWAY_PORT_ENV}, not {port}."
        raise ValueError(msg)
    state = config_lifecycle.app_state(api_app)
    state.tailnet_ssh_status = ssh.GatewayStatus()
    try:
        async with serve_listener(
            gateway_app(api_app, token),
            name="tailnet_ssh_gateway_listener",
            host=host,
            port=port,
            log_level=log_level,
            ws="none",
            access_log=False,
        ):
            yield
    finally:
        state.tailnet_ssh_status = None
