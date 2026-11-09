"""Runner-authenticated computer routes and filtered private RFB transport."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from mindroom.api.sandbox_runner import app_runner_token, validate_runner_token
from mindroom.api.worker_services import worker_services
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.worker_computer.protocol import ComputerStatus
from mindroom.worker_computer.rfb import RfbClientFilter, RfbProtocolError
from mindroom.worker_computer.runtime import ComputerControlError, WorkerComputerRuntime

logger = get_logger(__name__)

router = APIRouter(prefix="/computer", tags=["worker-computer"])


def _app_computer(app: FastAPI) -> WorkerComputerRuntime:
    """Return the explicitly installed lifespan-owned computer."""
    try:
        runtime = app.state.worker_computer
    except AttributeError:
        runtime = None
    if not isinstance(runtime, WorkerComputerRuntime):
        raise HTTPException(status_code=503, detail="Worker computer is not enabled on this dedicated worker.")
    return runtime


def _computer(request: Request) -> WorkerComputerRuntime:
    return _app_computer(request.app)


Computer = Annotated[WorkerComputerRuntime, Depends(_computer)]


class ComputerControlRequest(BaseModel):
    """An authenticated backend control action for one opaque viewer ID."""

    session_id: str = Field(min_length=1, max_length=256)
    action: Literal["take", "release", "stop"]
    generation: str | None = Field(default=None, min_length=1, max_length=256)


@router.get("", dependencies=[Depends(validate_runner_token)])
async def status(computer: Computer) -> ComputerStatus:
    """Inspect the current computer without restarting it."""
    return computer.status()


@router.post("/start", dependencies=[Depends(validate_runner_token)])
async def start(computer: Computer) -> ComputerStatus:
    """Start the worker display lazily."""
    try:
        return await computer.ensure_started()
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Worker computer display could not start.") from exc


@router.post("/control", dependencies=[Depends(validate_runner_token)])
async def control(payload: ComputerControlRequest, computer: Computer) -> ComputerStatus:
    """Apply exclusive takeover, release, or stop."""
    try:
        if payload.action == "take":
            return await computer.take_control(payload.session_id, generation=payload.generation)
        if payload.action == "release":
            return await computer.release_control(payload.session_id, generation=payload.generation)
        return await computer.stop(generation=payload.generation)
    except ComputerControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _client_to_display(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    computer: WorkerComputerRuntime,
    session_id: str,
    stream: asyncio.Event,
) -> None:
    parser = RfbClientFilter()
    while not stream.is_set():
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if not isinstance(data, bytes):
            raise WebSocketDisconnect(code=1003)
        filtered = parser.feed(data, allow_input=computer.allows_input(session_id, stream))
        if filtered and not stream.is_set():
            writer.write(filtered)
            await writer.drain()


async def _display_to_client(reader: asyncio.StreamReader, websocket: WebSocket) -> None:
    while data := await reader.read(65536):
        await websocket.send_bytes(data)


async def _attach_stream(
    websocket: WebSocket,
    session_id: str,
    generation: str,
) -> tuple[WorkerComputerRuntime, asyncio.Event] | None:
    """Lease one viewer's stream to the authenticated backend, or refuse the socket."""
    token = app_runner_token(websocket.app)
    supplied = websocket.headers.get("x-mindroom-sandbox-token", "")
    if (
        token
        and secrets.compare_digest(supplied, token)
        and 1 <= len(session_id) <= 256
        and 1 <= len(generation) <= 256
    ):
        try:
            computer = _app_computer(websocket.app)
            return computer, await computer.attach_stream(session_id, generation)
        except (HTTPException, ComputerControlError):
            pass
    await websocket.close(code=1008)
    return None


async def _end_stream(
    websocket: WebSocket,
    computer: WorkerComputerRuntime,
    session_id: str,
    lease: asyncio.Event,
    tasks: list[asyncio.Task[object]],
    close: Callable[[], Awaitable[None]],
) -> None:
    """Stop the stream's tasks and close what it holds, then release its lease, even under repeated cancellation."""

    async def cleanup() -> None:
        try:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await close()
            finally:
                try:
                    await computer.detach_stream(session_id, lease)
                finally:
                    with suppress(RuntimeError, OSError):
                        await websocket.close()

    try:
        await run_coroutine_until_complete(cleanup())
    except Exception as error:
        logger.warning("Worker computer stream cleanup failed", error_type=type(error).__name__)


@router.websocket("/stream")
async def stream(websocket: WebSocket, session_id: str, generation: str) -> None:
    """Bridge only this worker's private display with per-message input checks."""
    attached = await _attach_stream(websocket, session_id, generation)
    if attached is None:
        return
    computer, lease = attached
    tasks: list[asyncio.Task[object]] = []
    writer: asyncio.StreamWriter | None = None

    async def close_display() -> None:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    try:
        reader, writer = await asyncio.open_unix_connection(computer.display.socket_path)
        await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)
        tasks = [
            asyncio.create_task(_client_to_display(websocket, writer, computer, session_id, lease), name="viewer"),
            asyncio.create_task(_display_to_client(reader, websocket), name="display"),
            asyncio.create_task(lease.wait(), name="lease"),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        logger.info("Worker computer stream ended", ended_by=sorted(task.get_name() for task in done))
        for task in done:
            task.result()
    except RfbProtocolError as error:
        logger.warning("Worker computer stream rejected client input", reason=str(error))
    except (WebSocketDisconnect, OSError):
        pass
    except Exception as error:
        logger.warning("Worker computer stream failed", error_type=type(error).__name__)
    finally:
        await _end_stream(websocket, computer, session_id, lease, tasks, close_display)


async def _forward_commands(websocket: WebSocket, receive: Callable[[str], None]) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if not isinstance(text, str):
            raise WebSocketDisconnect(code=1003)
        receive(text)


async def _serve_agent_browser(websocket: WebSocket) -> None:
    """Give an admitted worker service its own CDP session on the agent's page; it acts only under the control lease.

    Opening it starts a stopped Computer, as creating a Computer session does; in a worker without a Computer, or one
    whose display cannot start, the handshake is refused.
    """
    try:
        computer = _app_computer(websocket.app)
        # Starting takes the lock an agent's browser call holds, so a running Computer is not started again.
        if computer.status()["state"] != "ready":
            await computer.ensure_started()
    except (HTTPException, OSError, RuntimeError, TimeoutError, ExceptionGroup):
        await websocket.close(code=1008)
        return
    # Playwright loads only once a service reaches the agent's browser, which already runs it.
    from mindroom.worker_computer.agent_browser import AgentBrowserBridge  # noqa: PLC0415

    bridge = AgentBrowserBridge(computer, websocket.send_text)
    tasks: list[asyncio.Task[None]] = []

    async def cleanup() -> None:
        try:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bridge.close()
        finally:
            with suppress(RuntimeError, OSError):
                await websocket.close()

    try:
        await websocket.accept()
        tasks = [
            asyncio.create_task(_forward_commands(websocket, bridge.receive), name="service"),
            asyncio.create_task(bridge.follow(), name="page"),
            asyncio.create_task(bridge.send(), name="answers"),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, OSError):
        pass
    except Exception as error:
        logger.warning("Agent browser bridge failed", error_type=type(error).__name__)
    finally:
        try:
            await run_coroutine_until_complete(cleanup())
        except Exception as error:
            logger.warning("Agent browser bridge cleanup failed", error_type=type(error).__name__)


@router.websocket("/agent-browser/{plugin}/{name}")
async def agent_browser(websocket: WebSocket, plugin: str, name: str) -> None:
    """Bridge the agent's browser page to the running instance of this service, which presents its own secret."""
    instance = worker_services(websocket.app).instance(plugin, name)
    supplied = websocket.headers.get("x-mindroom-service-secret", "").encode()
    if (
        instance is None
        or instance.process.returncode is not None
        or not secrets.compare_digest(supplied, instance.secret.encode())
    ):
        await websocket.close(code=1008)
        return
    await _serve_agent_browser(websocket)
