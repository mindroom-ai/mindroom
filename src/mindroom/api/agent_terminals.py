"""Chat clients' routes to the persistent terminals of the agents their caller may use in a room.

Each route signs its caller in with a client session, as plugin routes do, and admits them as ``RoomReader.agent()``
does; an agent is named by its Matrix user ID, and its terminals are those of its workspace as a turn of it for the
caller resolves it, so no path or worker comes from the client. Opening, closing and typing need ``terminal: type``.
A terminal's stream rechecks the caller as the Computer's stream does: what it may do, under the config served now.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.plugin_http import RoomReader, client_stream, require_room_reader, stream_room_reader
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.room_agent_access import room_execution_identity
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi.requests import HTTPConnection

    from mindroom.plugin_http import AgentWorkspace
    from mindroom.terminal.client import TerminalStream, WorkerTerminals
    from mindroom.terminal.session import TerminalInfo

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["terminals"])

# As often as the Computer's stream rechecks its viewer, and each check as long.
_RECHECK_SECONDS = 15.0
_NO_TYPING = "Typing in this agent's terminals needs a shell in a worker that mounts its workspace and power to post."
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
    data: str = Field(max_length=65536)


class _Resize(BaseModel):
    type: Literal["resize"]
    cols: _Cols
    rows: _Rows


_FRAME = TypeAdapter(Annotated[_Input | _Resize, Field(discriminator="type")])
_Room = Annotated[RoomReader, Depends(require_room_reader)]


class _StreamEnd(Exception):  # noqa: N818 - how a stream ends, not an error
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _workspace_terminals(
    connection: HTTPConnection,
    room_id: str,
    user_id: str,
    workspace: AgentWorkspace,
    *,
    create: bool,
) -> WorkerTerminals:
    """Return ``workspace``'s terminals as a turn of its agent for ``user_id`` in ``room_id`` resolves them."""
    paths = bind_current_request_snapshot(connection).runtime_paths
    requester_id = resolve_human_requester_alias(user_id, workspace.config, paths)
    identity = room_execution_identity(workspace.name, requester_id, room_id, paths)
    terminals = await asyncio.to_thread(
        agent_terminals,
        workspace.name,
        workspace.config,
        paths,
        identity,
        create=create,
    )
    if terminals is None:
        raise HTTPException(403, "This agent has no terminals.")
    return terminals


async def _typing_workspace(room: RoomReader, agent_user_id: str) -> AgentWorkspace:
    workspace = await room.agent(agent_user_id)
    if workspace.terminal != "type":
        raise HTTPException(403, _NO_TYPING)
    return workspace


def _terminal(workspace: AgentWorkspace, info: TerminalInfo) -> _Terminal:
    return _Terminal(agent_user_id=workspace.user_id, writable=workspace.terminal == "type", **asdict(info))


@router.get("/terminals")
async def list_terminals(request: Request, room: _Room, room_id: str) -> _TerminalList:
    """List the terminals of each workspace the caller may watch in the room, oldest first per agent."""

    async def listed(workspace: AgentWorkspace) -> list[_Terminal]:
        terminals = await _workspace_terminals(request, room_id, room.user.user_id, workspace, create=False)
        try:
            return [_terminal(workspace, info) for info in await terminals.sessions()]
        except TerminalError as error:
            raise HTTPException(error.status_code, error.detail) from None

    workspaces = [workspace for workspace in await room.agents() if workspace.terminal != "none"]
    found = await asyncio.gather(*map(listed, workspaces))
    return _TerminalList(terminals=[terminal for terminals in found for terminal in terminals])


@router.post("/agents/{agent_user_id}/terminals", status_code=201)
async def open_terminal(
    request: Request,
    room: _Room,
    room_id: str,
    agent_user_id: str,
    size: _OpenTerminal | None = None,
) -> _Terminal:
    """Open a terminal in the agent's workspace, starting its worker as a shell call would."""
    workspace = await _typing_workspace(room, agent_user_id)
    terminals = await _workspace_terminals(request, room_id, room.user.user_id, workspace, create=True)
    size = size or _OpenTerminal()
    try:
        return _terminal(workspace, await terminals.open(cols=size.cols, rows=size.rows))
    except TerminalError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.delete("/agents/{agent_user_id}/terminals/{terminal_id}", status_code=204)
async def close_terminal(
    request: Request,
    room: _Room,
    room_id: str,
    agent_user_id: str,
    terminal_id: str,
) -> Response:
    """Close one of the agent's terminals; one the workspace does not have answers 404."""
    workspace = await _typing_workspace(room, agent_user_id)
    terminals = await _workspace_terminals(request, room_id, room.user.user_id, workspace, create=False)
    try:
        await terminals.close(terminal_id)
    except TerminalError as error:
        raise HTTPException(error.status_code, error.detail) from None
    return Response(status_code=204)


@router.websocket("/agents/{agent_user_id}/terminals/{terminal_id}")
async def terminal_stream(websocket: WebSocket, room_id: str, agent_user_id: str, terminal_id: str) -> None:
    """Relay one terminal: its scrollback, then live output, as binary frames, and the viewer's keys and size.

    Text frames tell the viewer ``{"type": "writable", "writable"}``, first and whenever it changes, and
    ``{"type": "exit", "code"}`` when the shell exits; the viewer sends ``{"type": "input", "data"}`` and
    ``{"type": "resize", "cols", "rows"}``, which are dropped while it may only watch.
    """
    async with client_stream(websocket, room_id) as user:
        try:
            workspace = await (await stream_room_reader(websocket, user, room_id)).agent(agent_user_id)
            terminals = await _workspace_terminals(websocket, room_id, user.user_id, workspace, create=False)
            async with terminals.attach(terminal_id) as terminal:
                await _Viewer(websocket, terminal, workspace).relay(
                    lambda: stream_room_reader(websocket, user, room_id),
                )
        except _StreamEnd as end:
            await _close(websocket, end.code, end.reason)
        except (HTTPException, TerminalError) as error:
            refused = error.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
            code = status.WS_1008_POLICY_VIOLATION if refused else status.WS_1013_TRY_AGAIN_LATER
            await _close(websocket, code, error.detail)
        except TerminalDetachedError:
            await _close(websocket, status.WS_1013_TRY_AGAIN_LATER, "The terminal's stream ended; attach again.")
        except WebSocketDisconnect:
            pass


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    # The client may be gone already, and a close frame holds at most 123 bytes of reason.
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code, reason.encode()[:123].decode(errors="ignore"))


class _Viewer:
    """One stream's viewer: the terminal's output to it, its keys to the terminal, and rechecks of what it may do."""

    def __init__(self, websocket: WebSocket, terminal: TerminalStream, workspace: AgentWorkspace) -> None:
        self._websocket = websocket
        self._terminal = terminal
        self._workspace = workspace
        self._writable = workspace.terminal == "type"
        self._sending = asyncio.Lock()

    async def relay(self, room: Callable[[], Awaitable[RoomReader]]) -> None:
        """Relay until the shell exits or the viewer leaves, rechecking in ``room`` what the viewer may do."""
        await self._send({"type": "writable", "writable": self._writable})
        tasks = [asyncio.create_task(task) for task in (self._output(), self._keys(), self._recheck(room))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
        raise _StreamEnd(status.WS_1000_NORMAL_CLOSURE, "The terminal's shell exited.")

    async def _keys(self) -> None:
        while True:
            try:
                frame = _FRAME.validate_json(await self._websocket.receive_text())
            except (ValidationError, KeyError):
                raise _StreamEnd(status.WS_1003_UNSUPPORTED_DATA, "Unsupported frame.") from None
            if not self._writable:
                continue
            if isinstance(frame, _Input):
                await self._terminal.send_input(frame.data)
            else:
                await self._terminal.resize(frame.cols, frame.rows)

    async def _recheck(self, room: Callable[[], Awaitable[RoomReader]]) -> None:
        while True:
            await asyncio.sleep(_RECHECK_SECONDS)
            try:
                async with asyncio.timeout(_RECHECK_SECONDS):
                    current = await (await room()).agent(self._workspace.user_id)
            except TimeoutError:
                raise _StreamEnd(status.WS_1013_TRY_AGAIN_LATER, "Agent access is unavailable; try again.") from None
            if current.terminal == "none" or current.root != self._workspace.root:
                raise _StreamEnd(status.WS_1008_POLICY_VIOLATION, "This terminal is no longer open to you.")
            if (current.terminal == "type") != self._writable:
                self._writable = not self._writable
                await self._send({"type": "writable", "writable": self._writable})
