"""Runner routes of persistent terminals: shells in a pty beside the shell commands this sandbox runner runs.

The primary (``mindroom.terminal.client``) opens a terminal with the request a shell call would send; the runner
prepares that request as ``/execute`` would and starts the shell toolkit's interactive Bash in a pty here
(``prepare_terminal_launch``), so a terminal runs where, and as, the agent's commands run: in this worker, with its
environment, workspace, limits and egress rules.

Every route needs the runner token (the ``X-Mindroom-Sandbox-Token`` header, also on the websocket) and names the
terminal's ``owner``; a terminal answers only to its owner. The websocket sends output as binary frames, the
scrollback first, and the shell's exit as the text frame ``{"type": "exit", "code"}``; it takes the text frames
``{"type": "input", "data"}`` and ``{"type": "resize", "cols", "rows"}``. It closes without an exit frame when its
viewer falls too far behind, and attaching again replays the scrollback.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from mindroom.api.sandbox_runner import prepare_terminal_launch, validate_runner_token
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.terminal.session import (
    TerminalError,
    TerminalInfo,
    TerminalManager,
    TerminalSession,
    TerminalViewer,
)
from mindroom.workers.protocol import ExecuteRequest  # noqa: TC001 - pydantic reads it at runtime

router = APIRouter(prefix="/api/sandbox-runner/terminal", tags=["worker-terminal"])
# The runner's middleware checks the token of every HTTP request too; the websocket checks its own.
_GUARDED = [Depends(validate_runner_token)]

_Owner = Annotated[str, Query(min_length=1, max_length=128)]


class _Open(BaseModel):
    request: ExecuteRequest
    cols: int = Field(default=80, ge=1, le=10000)
    rows: int = Field(default=24, ge=1, le=10000)
    cwd: str | None = Field(default=None, max_length=4096)


class _Input(BaseModel):
    type: Literal["input"]
    data: str


class _Resize(BaseModel):
    type: Literal["resize"]
    cols: int = Field(ge=1, le=10000)
    rows: int = Field(ge=1, le=10000)


_FRAME: TypeAdapter[_Input | _Resize] = TypeAdapter(Annotated[_Input | _Resize, Field(discriminator="type")])


def _terminals(app: FastAPI) -> TerminalManager:
    """Return this runner's terminals, created on first use."""
    try:
        return app.state.worker_terminals
    except AttributeError:
        manager = app.state.worker_terminals = TerminalManager()
        return manager


async def close_worker_terminals(app: FastAPI) -> None:
    """End this runner's terminals as it stops."""
    await _terminals(app).close_all()


def _refusal(error: TerminalError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


@router.post("/sessions", dependencies=_GUARDED)
async def open_terminal(request: Request, payload: _Open, owner: _Owner) -> TerminalInfo:
    """Start a terminal from a shell call's request."""
    try:
        launch = await asyncio.to_thread(prepare_terminal_launch, request.app, payload.request, payload.cwd)
        return _terminals(request.app).open(owner, launch, cols=payload.cols, rows=payload.rows).info()
    except TerminalError as error:
        raise _refusal(error) from None


@router.get("/sessions", dependencies=_GUARDED)
async def list_terminals(request: Request, owner: _Owner) -> list[TerminalInfo]:
    """Describe the owner's terminals, oldest first."""
    return [session.info() for session in _terminals(request.app).sessions(owner)]


@router.delete("/sessions/{session_id}", status_code=204, dependencies=_GUARDED)
async def close_terminal(request: Request, session_id: str, owner: _Owner) -> None:
    """End a terminal."""
    try:
        await _terminals(request.app).close(owner, session_id)
    except TerminalError as error:
        raise _refusal(error) from None


async def _viewer_input(websocket: WebSocket, session: TerminalSession) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is None:
            continue
        frame = _FRAME.validate_json(text)
        if isinstance(frame, _Input):
            session.write(frame.data.encode())
        else:
            session.resize(frame.cols, frame.rows)


async def _viewer_output(websocket: WebSocket, scrollback: bytes, viewer: TerminalViewer) -> None:
    if scrollback:
        await websocket.send_bytes(scrollback)
    while (event := await viewer.next()).type == "output":
        await websocket.send_bytes(event.data)
    # A dropped viewer gets no exit frame, which tells it to attach again.
    if event.type == "exit":
        await websocket.send_json({"type": "exit", "code": event.code})


@router.websocket("/sessions/{session_id}/io")
async def terminal_io(websocket: WebSocket, session_id: str, owner: str) -> None:
    """Bridge one viewer to a terminal: its scrollback, then live output and its exit; and its keys and size."""
    try:
        await validate_runner_token(websocket, websocket.headers.get("x-mindroom-sandbox-token"))
        session = _terminals(websocket.app).get(owner, session_id)
    except (HTTPException, TerminalError):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    scrollback, viewer = session.attach()
    tasks = [
        asyncio.create_task(_viewer_input(websocket, session), name="terminal-viewer-input"),
        asyncio.create_task(_viewer_output(websocket, scrollback, viewer), name="terminal-viewer-output"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, TerminalError, ValidationError, OSError, RuntimeError):
        pass
    finally:

        async def cleanup() -> None:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            session.detach(viewer)
            with contextlib.suppress(RuntimeError, OSError):
                await websocket.close()

        # A cancelled connection still detaches its viewer and closes before the cancellation goes on.
        await run_coroutine_until_complete(cleanup())
