"""Runner-authenticated computer routes and filtered private RFB transport."""

import asyncio
import secrets
from contextlib import suppress
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from mindroom.api.sandbox_runner import app_runner_token, validate_runner_token
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


@router.websocket("/stream")
async def stream(websocket: WebSocket, session_id: str, generation: str) -> None:  # noqa: C901 - one owned stream lifecycle
    """Bridge only this worker's private display with per-message input checks."""
    token = app_runner_token(websocket.app)
    supplied = websocket.headers.get("x-mindroom-sandbox-token", "")
    if not token or not secrets.compare_digest(supplied, token):
        await websocket.close(code=1008)
        return
    if not 1 <= len(session_id) <= 256 or not 1 <= len(generation) <= 256:
        await websocket.close(code=1008)
        return
    try:
        computer = _app_computer(websocket.app)
        lease = await computer.attach_stream(session_id, generation)
    except (HTTPException, ComputerControlError):
        await websocket.close(code=1008)
        return
    tasks: list[asyncio.Task[object]] = []
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(computer.display.socket_path)
        await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)
        tasks = [
            asyncio.create_task(_client_to_display(websocket, writer, computer, session_id, lease)),
            asyncio.create_task(_display_to_client(reader, websocket)),
            asyncio.create_task(lease.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, OSError, RfbProtocolError):
        pass
    except Exception as error:
        logger.warning("Worker computer stream failed", error_type=type(error).__name__)
    finally:

        async def cleanup() -> None:
            try:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                try:
                    if writer is not None:
                        writer.close()
                        with suppress(OSError):
                            await writer.wait_closed()
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
