"""Chat clients' routes to the persistent terminals of the agents their caller may use in a room.

``docs/architecture/terminals.md`` describes the routes, the stream, and who may open, close, and type.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mindroom.api import agent_streams
from mindroom.plugin_http import RoomReader, client_stream, require_room_reader, stream_room_reader
from mindroom.terminal.client import MAX_INPUT_CHARS, TerminalDetachedError
from mindroom.terminal.session import TerminalError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.plugin_http import AgentWorkspace
    from mindroom.terminal.client import TerminalStream, WorkerTerminals
    from mindroom.terminal.session import TerminalInfo

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["terminals"])

_NO_TERMINALS = "This agent has no terminals."
_NO_TYPING = (
    "Typing in this agent's terminals needs `terminals: type` in its config, a shell in a worker that mounts its "
    "workspace, and power to post."
)
_Cols = Annotated[int, Field(ge=2, le=1000)]
_Rows = Annotated[int, Field(ge=1, le=500)]


class _Terminal(BaseModel):
    """One terminal of an agent's workspace, and whether the caller may type in it."""

    agent_user_id: str
    id: str
    cols: int
    rows: int
    alive: bool
    exit_code: int | None
    created_at: float
    writable: bool


class _TerminalList(BaseModel):
    """The terminals of the workspaces the caller may watch in a room."""

    terminals: list[_Terminal]


class _OpenTerminal(BaseModel):
    """The size a new terminal starts with."""

    model_config = ConfigDict(extra="forbid")
    cols: _Cols = 80
    rows: _Rows = 24


class _Input(BaseModel):
    type: Literal["input"]
    data: str = Field(max_length=MAX_INPUT_CHARS)


class _Resize(BaseModel):
    type: Literal["resize"]
    cols: _Cols
    rows: _Rows


_FRAME = TypeAdapter(Annotated[_Input | _Resize, Field(discriminator="type")])
_Room = Annotated[RoomReader, Depends(require_room_reader)]


async def _workspace_terminals(room: RoomReader, workspace: AgentWorkspace, *, create: bool) -> WorkerTerminals:
    terminals = await room.terminals(workspace, create=create)
    if terminals is None:
        raise HTTPException(403, _NO_TERMINALS)
    return terminals


async def _typing_workspace(room: RoomReader, agent_user_id: str) -> AgentWorkspace:
    workspace = await room.agent(agent_user_id)
    if workspace.terminal != "type":
        raise HTTPException(403, _NO_TYPING)
    return workspace


def _terminal(workspace: AgentWorkspace, info: TerminalInfo) -> _Terminal:
    return _Terminal(agent_user_id=workspace.user_id, writable=workspace.terminal == "type", **asdict(info))


@router.get("/terminals")
async def list_terminals(room: _Room) -> _TerminalList:
    """List the terminals of each workspace the caller may watch in the room, oldest first per agent."""

    async def listed(workspace: AgentWorkspace) -> list[_Terminal]:
        terminals = await _workspace_terminals(room, workspace, create=False)
        try:
            return [_terminal(workspace, info) for info in await terminals.sessions()]
        except TerminalError as error:
            raise HTTPException(error.status_code, error.detail) from None

    workspaces = [workspace for workspace in await room.agents() if workspace.terminal != "none"]
    found = await asyncio.gather(*map(listed, workspaces))
    return _TerminalList(terminals=[terminal for terminals in found for terminal in terminals])


@router.post("/agents/{agent_user_id}/terminals", status_code=201)
async def open_terminal(room: _Room, agent_user_id: str, size: _OpenTerminal | None = None) -> _Terminal:
    """Open a terminal in the agent's workspace, starting its worker as a shell call would."""
    workspace = await _typing_workspace(room, agent_user_id)
    terminals = await _workspace_terminals(room, workspace, create=True)
    size = size or _OpenTerminal()
    try:
        return _terminal(workspace, await terminals.open(cols=size.cols, rows=size.rows))
    except TerminalError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/agents/{agent_user_id}/terminals/{terminal_id}", status_code=204)
async def close_terminal(room: _Room, agent_user_id: str, terminal_id: str) -> Response:
    """Close one of the agent's terminals; one the workspace does not have answers 404."""
    workspace = await _typing_workspace(room, agent_user_id)
    terminals = await _workspace_terminals(room, workspace, create=False)
    try:
        await terminals.close(terminal_id)
    except TerminalError as error:
        raise HTTPException(error.status_code, error.detail) from None
    return Response(status_code=204)


@router.websocket("/agents/{agent_user_id}/terminals/{terminal_id}")
async def terminal_stream(websocket: WebSocket, room_id: str, agent_user_id: str, terminal_id: str) -> None:
    """Relay one terminal's output to the viewer and, while it may type, its keys and size to the terminal."""
    async with client_stream(websocket, room_id) as user:
        try:
            room = await stream_room_reader(websocket, user, room_id)
            workspace = await room.agent(agent_user_id)
            terminals = await _workspace_terminals(room, workspace, create=False)
            async with terminals.attach(terminal_id) as terminal:
                await _Viewer(websocket, terminal, workspace).relay(
                    agent_streams.rechecked(websocket, user, room_id, agent_user_id),
                )
        except WebSocketException as end:
            await agent_streams.close(websocket, end.code, end.reason)
        except (HTTPException, TerminalError) as error:
            await agent_streams.close_answering(websocket, error.status_code, error.detail)
        except TerminalDetachedError:
            await agent_streams.close(
                websocket,
                status.WS_1013_TRY_AGAIN_LATER,
                "The terminal's stream ended; attach again.",
            )
        except WebSocketDisconnect:
            pass


class _Viewer:
    """One stream's viewer: the terminal's output to it, its keys to the terminal, and rechecks of what it may do."""

    def __init__(self, websocket: WebSocket, terminal: TerminalStream, workspace: AgentWorkspace) -> None:
        self._websocket = websocket
        self._terminal = terminal
        self._root = workspace.root
        self._writable = workspace.terminal == "type"
        self._sending = asyncio.Lock()

    async def relay(self, rechecked: AsyncIterator[AgentWorkspace]) -> None:
        """Relay until the shell exits or the viewer leaves, following in ``rechecked`` what the viewer may do."""
        await self._send({"type": "writable", "writable": self._writable})
        await agent_streams.until_first(self._output(), self._keys(), self._recheck(rechecked))

    async def _send(self, frame: dict[str, object] | bytes) -> None:
        async with self._sending:
            if isinstance(frame, bytes):
                await self._websocket.send_bytes(frame)
            else:
                await self._websocket.send_json(frame)

    async def _output(self) -> None:
        while (event := await self._terminal.receive()).type == "output":
            await self._send(event.data)
        await self._send({"type": "exit", "code": event.code})
        raise WebSocketException(status.WS_1000_NORMAL_CLOSURE, "The terminal's shell exited.")

    async def _keys(self) -> None:
        while True:
            try:
                frame = _FRAME.validate_json(await self._websocket.receive_text())
            except (ValidationError, KeyError):
                raise WebSocketException(status.WS_1003_UNSUPPORTED_DATA, "Unsupported frame.") from None
            if not self._writable:
                continue
            if isinstance(frame, _Input):
                await self._terminal.send_input(frame.data)
            else:
                await self._terminal.resize(frame.cols, frame.rows)

    async def _recheck(self, rechecked: AsyncIterator[AgentWorkspace]) -> None:
        async for current in rechecked:
            if current.terminal == "none" or current.root != self._root:
                raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "This terminal is no longer open to you.")
            if (current.terminal == "type") != self._writable:
                self._writable = not self._writable
                await self._send({"type": "writable", "writable": self._writable})
