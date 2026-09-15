"""Matrix-authenticated public Computer session gateway."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from nio.exceptions import ProtocolError
from pydantic import BaseModel, ConfigDict, Field

from mindroom.api import config_lifecycle
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.constants import RuntimePaths, runtime_env_flag
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.worker_computer.auth import MatrixOpenIDToken, computer_origins, verify_openid
from mindroom.worker_computer.client import computer_request, computer_stream
from mindroom.worker_computer.sessions import ComputerError, ComputerSession, ComputerSessionStore, ComputerTarget
from mindroom.workers.backend import WorkerBackend, WorkerBackendError
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from mindroom.config.main import Config
    from mindroom.worker_computer.protocol import ComputerStatus
    from mindroom.workers.models import WorkerHandle

_STREAM_RECHECK_SECONDS = 25.0


@dataclass(frozen=True)
class ComputerRuntime:
    """The only orchestrator collaborator exposed to public routes."""

    authorize: Callable[[str, str, str], Awaitable[ComputerTarget]]
    config_generation: int
    config: Config = field(repr=False)


def rebind_computer_runtime(app: FastAPI, preload: config_lifecycle.ApiSnapshot, *, loaded: bool) -> None:
    """Keep a prebound authorizer across initial API configuration publication."""
    state = config_lifecycle.app_state(app)
    runtime = state.computer_runtime
    if runtime is None or preload.runtime_config is not None or preload.config_load_result is not None:
        return
    if loaded:
        state.computer_runtime = replace(
            runtime,
            config_generation=config_lifecycle.require_api_state(app).snapshot.generation,
        )
    else:
        state.computer_runtime = None
        if state.computer_sessions is not None:
            state.computer_sessions.close_all()


class _ComputerRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except ComputerError as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
            except RequestValidationError:
                # FastAPI's default validation response echoes input, including credentials.
                response = JSONResponse({"detail": "Invalid computer request."}, status_code=401)
            except HTTPException as error:
                response = JSONResponse({"detail": "Computer runtime is unavailable."}, status_code=error.status_code)
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


logger = get_logger(__name__)

router = APIRouter(prefix="/api/computers", tags=["computers"], route_class=_ComputerRoute)


class _CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openid_token: MatrixOpenIDToken
    room_id: str = Field(min_length=1, max_length=255, pattern=r"^!")
    agent_user_id: str = Field(min_length=1, max_length=255, pattern=r"^@")


class _Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["take", "release", "stop"]


def _store(app: FastAPI) -> ComputerSessionStore:
    state = config_lifecycle.ensure_app_state(app)
    if state.computer_sessions is None:
        state.computer_sessions = ComputerSessionStore()
    return state.computer_sessions


def _runtime(connection: Request | WebSocket) -> ComputerRuntime:
    state = config_lifecycle.app_state(connection.app)
    snapshot = config_lifecycle.require_api_state(connection.app).snapshot
    runtime = state.computer_runtime
    if runtime is None or not runtime_env_flag(
        WORKER_COMPUTER_ENABLED_ENV,
        runtime_paths=snapshot.runtime_paths,
    ):
        raise ComputerError(503, "Computer authorization runtime is unavailable.")
    if (
        runtime.config_generation != snapshot.generation
        or snapshot.runtime_config is None
        or runtime.config.model_dump() != snapshot.runtime_config.model_dump()
    ):
        raise ComputerError(409, "Computer configuration changed; create a new session.")
    return runtime


def _session(request: Request, session_id: str) -> ComputerSession:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise ComputerError(401, "A computer session bearer is required.")
    return _store(request.app).authenticate(session_id, authorization[7:])


def _resolve_worker(config: Config, paths: RuntimePaths, target: ComputerTarget, *, start: bool) -> WorkerHandle:
    try:
        lease = lease_configured_primary_worker_manager(paths, runtime_config=config)
        if lease is None:
            raise ComputerError(503, "A dedicated computer worker backend is required.")
        with lease as manager:
            if manager.backend_name not in {"docker", "kubernetes"}:
                raise ComputerError(503, "Computer requires a dedicated Docker or Kubernetes backend.")
            handle = manager.ensure_worker(target.spec) if start else manager.touch_worker(target.spec.worker_key)
        if handle is None or handle.status != "ready" or not handle.auth_token:
            raise ComputerError(503, "Computer worker is unavailable; create a new session to restart.")
    except WorkerBackendError:
        raise ComputerError(503, "Computer worker backend is unavailable.") from None
    return handle


def _same_worker(previous: WorkerHandle, current: WorkerHandle) -> bool:
    return (
        previous.worker_id,
        previous.endpoint,
        previous.auth_token,
        previous.last_started_at,
        previous.startup_count,
    ) == (current.worker_id, current.endpoint, current.auth_token, current.last_started_at, current.startup_count)


async def _authorized_target(
    runtime: ComputerRuntime,
    requester_id: str,
    room_id: str,
    agent_user_id: str,
) -> ComputerTarget:
    try:
        async with asyncio.timeout(20):
            return await runtime.authorize(requester_id, room_id, agent_user_id)
    except (aiohttp.ClientError, ProtocolError, OSError):
        # Matrix transport errors may contain credential-bearing request URLs.
        raise ComputerError(503, "Computer authorization is unavailable.") from None


async def _authorize(connection: Request | WebSocket, target: ComputerTarget) -> None:
    runtime = _runtime(connection)
    current = await _authorized_target(runtime, target.requester_id, target.room_id, target.agent_user_id)
    if runtime is not _runtime(connection) or current != target:
        raise ComputerError(409, "Computer configuration or scope changed; create a new session.")


async def _checked_status(connection: Request | WebSocket, session: ComputerSession) -> ComputerStatus:
    store = _store(connection.app)
    try:
        store.get(session.session_id)
        await _authorize(connection, session.target)
        config, paths = config_lifecycle.read_app_committed_runtime_config(connection.app)
        handle = await asyncio.to_thread(_resolve_worker, config, paths, session.target, start=False)
        if session.handle is None or not _same_worker(session.handle, handle):
            raise ComputerError(409, "Computer worker changed; create a new session.")  # noqa: TRY301 - revoke below
        status = await computer_request(handle, "status", session.session_id)
        if status["generation"] != session.generation:
            raise ComputerError(409, "Computer stopped or restarted; create a new session.")  # noqa: TRY301 - revoke below
        store.get(session.session_id)
    except (ComputerError, HTTPException):
        store.close(session.session_id)
        raise
    return status


def _public_status(session: ComputerSession, status: ComputerStatus) -> dict[str, str | float]:
    return {
        "session_id": session.session_id,
        "state": status["state"],
        "mode": "control" if status["controller_session_id"] == session.session_id else "view",
        "expires_at": session.expires_at,
    }


@router.post("/sessions")
async def create_session(payload: _CreateSession, request: Request) -> dict[str, str | float]:
    """Exchange configured-homeserver OpenID for a requester-bound computer viewer."""
    runtime = _runtime(request)
    config, paths = config_lifecycle.read_app_committed_runtime_config(request.app)
    requester_id = await verify_openid(payload.openid_token, paths)
    target = await _authorized_target(runtime, requester_id, payload.room_id, payload.agent_user_id)
    if runtime is not _runtime(request):
        raise ComputerError(409, "Computer configuration changed; retry session creation.")
    store = _store(request.app)
    session = store.create(target)
    try:
        session.handle = await asyncio.to_thread(_resolve_worker, config, paths, target, start=True)
        status = await computer_request(session.handle, "start", session.session_id)
        session.generation = status["generation"]
        await _authorize(request, target)
        store.get(session.session_id)
    except BaseException:
        store.close(session.session_id)
        raise
    return {**_public_status(session, status), "session_token": session.session_token}


@router.get("/sessions/{session_id}")
async def session_status(request: Request, session_id: str) -> dict[str, str | float]:
    """Inspect without starting a stopped computer."""
    session = _session(request, session_id)
    return _public_status(session, await _checked_status(request, session))


@router.post("/sessions/{session_id}/stream-ticket")
async def stream_ticket(request: Request, session_id: str) -> dict[str, str | float]:
    """Mint a single-use upgrade credential after current authorization checks."""
    session = _session(request, session_id)
    await _checked_status(request, session)
    ticket = _store(request.app).issue_stream_ticket(session.session_id)
    return {"ticket": ticket.ticket, "expires_at": ticket.expires_at}


@router.post("/sessions/{session_id}/control")
async def control(payload: _Control, request: Request, session_id: str) -> dict[str, str | float]:
    """Take or release browser control, or stop and invalidate this runtime generation."""
    session = _session(request, session_id)
    await _checked_status(request, session)
    assert session.handle is not None
    store = _store(request.app)
    try:
        status = await run_coroutine_until_complete(
            computer_request(session.handle, payload.action, session.session_id, generation=session.generation),
            on_cancelled=lambda: store.close(session.session_id),
        )
        store.get(session.session_id)
    except BaseException:
        if payload.action == "take":
            # A delayed worker take can settle after local revocation. Drain its
            # compensation before reporting failure, even if our caller cancels.
            with suppress(ComputerError):
                await run_coroutine_until_complete(
                    computer_request(session.handle, "release", session.session_id, generation=session.generation),
                )
        raise
    if payload.action == "stop":
        store.close(session.session_id)
    return _public_status(session, status)


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> Response:
    """Revoke viewer credentials and close its stream, releasing input ownership."""
    session = _session(request, session_id)
    # Deletion must still clean up after authorization has been revoked.
    _store(request.app).close(session.session_id)
    if session.handle is not None:
        with suppress(ComputerError):
            await computer_request(session.handle, "release", session.session_id, generation=session.generation)
    return Response(status_code=204)


def active_computer_worker_keys(app: FastAPI) -> frozenset[str]:
    """Collect live Computer worker keys on the app's owning event loop."""
    sessions = config_lifecycle.app_state(app).computer_sessions
    return sessions.active_worker_keys() if sessions is not None else frozenset()


def touch_computer_workers(app: FastAPI, manager: WorkerBackend) -> None:
    """Keep live streams active during manual cleanup on the owning loop."""
    for worker_key in active_computer_worker_keys(app):
        manager.touch_worker(worker_key)


async def _upstream(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if not isinstance(data, bytes):
            raise WebSocketDisconnect(code=1003)
        await upstream.send_bytes(data)


async def _downstream(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> None:
    async for message in upstream:
        if message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
        else:
            return


async def _maintain(websocket: WebSocket, session: ComputerSession, stream: asyncio.Event) -> None:
    while not stream.is_set():
        started = asyncio.get_running_loop().time()
        try:
            # A 25-second start cadence plus at most five seconds for the entire
            # authorization/manager/status check bounds completed touches to 30s.
            async with asyncio.timeout(5):
                await _checked_status(websocket, session)
        except TimeoutError:
            _store(websocket.app).close(session.session_id)
            return
        interval = max(0, _STREAM_RECHECK_SECONDS - (asyncio.get_running_loop().time() - started))
        timeout = min(interval, max(0, session.expires_at - _store(websocket.app).clock()))
        try:
            await asyncio.wait_for(stream.wait(), timeout=timeout)
        except TimeoutError:
            continue


def _stream_session(websocket: WebSocket, session_id: str) -> ComputerSession:
    paths = config_lifecycle.require_api_state(websocket.app).snapshot.runtime_paths
    if websocket.headers.get("origin") not in computer_origins(paths):
        raise ComputerError(403, "Computer stream origin is not allowed.")
    protocols = websocket.scope.get("subprotocols", [])
    tickets = [value.removeprefix("mindroom-ticket.") for value in protocols if value.startswith("mindroom-ticket.")]
    if "binary" not in protocols or len(tickets) != 1:
        raise ComputerError(401, "A computer stream ticket is required.")
    return _store(websocket.app).consume_stream_ticket(session_id, tickets[0])


@router.websocket("/sessions/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str) -> None:
    """Consume a subprotocol ticket; only backend headers carry worker credentials."""
    tasks: list[asyncio.Task[None]] = []
    session: ComputerSession | None = None
    stream_closed = asyncio.Event()
    accepted = False
    try:
        session = _stream_session(websocket, session_id)
        await _checked_status(websocket, session)
        if session.stream is not None:
            session.stream.set()
        session.stream = stream_closed
        assert session.handle is not None
        assert session.generation is not None
        async with computer_stream(session.handle, session.session_id, session.generation) as upstream:
            await websocket.accept(subprotocol="binary", headers=[(b"cache-control", b"no-store")])
            accepted = True
            tasks = [
                asyncio.create_task(_upstream(websocket, upstream)),
                asyncio.create_task(_downstream(websocket, upstream)),
                asyncio.create_task(_maintain(websocket, session, stream_closed)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except ComputerError as error:
        if not accepted:
            await websocket.send_denial_response(
                JSONResponse(
                    {"detail": error.detail},
                    status_code=error.status_code,
                    headers={"Cache-Control": "no-store"},
                ),
            )
    except (HTTPException, WebSocketDisconnect, OSError, aiohttp.ClientError):
        pass
    except Exception as error:
        logger.warning("Computer stream failed", error_type=type(error).__name__)
    finally:
        stream_closed.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if session is not None and session.stream is stream_closed:
            session.stream = None
        # Closing the upstream stream releases its exact worker-side ownership.
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1008)
