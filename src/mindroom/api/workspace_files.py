"""Chat clients' routes to the workspaces of a room's agents: list them, read and change their files, follow changes.

Every route signs its caller in with a client session and checks them through ``RoomReader`` on each request, so a
workspace opens only while the room and the agent's access rules let the caller use that agent there. The agent's user
ID names the workspace; its folder is the one a turn of that agent for this caller in this room uses, never a path
from the request, and file paths are relative to it (see ``mindroom.workspace_files.operations``). The change stream
rechecks the caller as a terminal's stream does.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator

from mindroom.api import agent_streams
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.plugin_http import (
    AgentWorkspace,
    RoomReader,
    client_stream,
    require_room_reader,
    stream_room_reader,
)
from mindroom.room_agent_access import room_execution_identity
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.workspace_files import operations
from mindroom.workspace_files.watch import workspace_changes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["workspace-files"])

_Room = Annotated[RoomReader, Depends(require_room_reader)]


@dataclass(frozen=True)
class OpenWorkspace:
    """A workspace the caller may open in the room, named by its agent."""

    agent_user_id: str
    display_name: str
    private: bool
    """Whether the agent is private, so this caller's workspace is their own."""
    files: Literal["read", "write"]
    terminal: Literal["none", "watch", "type"]
    """What the caller may do in the agent's terminals, as the terminal routes allow it."""


class _Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    etag: str | None = None
    create: bool = False

    @model_validator(mode="after")
    def _create_or_etag(self) -> Self:
        if self.create and self.etag is not None:
            msg = "A save that creates the file can't also name its etag."
            raise ValueError(msg)
        return self


class _Path(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class _Move(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    to: str


def _with_files(workspace: AgentWorkspace) -> AgentWorkspace:
    if workspace.files == "none":
        raise HTTPException(403, "This agent's tools reach no files.")
    return workspace


async def _opened(room: RoomReader, agent_user_id: str) -> AgentWorkspace:
    return _with_files(await room.agent(agent_user_id))


async def _writable(room: RoomReader, agent_user_id: str) -> AgentWorkspace:
    workspace = await _opened(room, agent_user_id)
    if workspace.files != "write":
        raise HTTPException(403, "You may only read this workspace.")
    return workspace


async def _made_root(request: Request, room: RoomReader, room_id: str, agent_user_id: str) -> Path:
    """Return the root of a workspace the caller may change, made as the agent's first turn for them would make it."""
    workspace = await _writable(room, agent_user_id)
    if workspace.root.is_dir():
        return workspace.root
    paths = bind_current_request_snapshot(request).runtime_paths
    identity = room_execution_identity(workspace.name, room.user.user_id, room_id, workspace.config, paths)
    # The same config, paths and identity the room's check resolved this root from.
    await asyncio.to_thread(resolve_agent_runtime, workspace.name, workspace.config, paths, identity, create=True)
    return workspace.root


@router.get("/workspaces")
async def list_workspaces(room: _Room) -> dict[str, list[OpenWorkspace]]:
    """Return the workspaces the caller may open in the room, in config order, with what they may do in each."""
    return {
        "workspaces": [
            OpenWorkspace(
                workspace.user_id,
                workspace.display_name,
                workspace.private,
                workspace.files,
                workspace.terminal,
            )
            for workspace in await room.agents()
            if workspace.files != "none"
        ],
    }


@router.get("/agents/{agent_user_id}/workspace/folder")
async def list_folder(room: _Room, agent_user_id: str, path: str = "") -> operations.Folder:
    """Return one folder's entries, folders first; the root lists empty before the agent's first turn."""
    workspace = await _opened(room, agent_user_id)
    return await asyncio.to_thread(operations.list_folder, workspace.root, path)


@router.get("/agents/{agent_user_id}/workspace/file")
async def read_file(room: _Room, agent_user_id: str, path: str) -> operations.FileContent:
    """Return a file's content with its etag, answering 413 for one over 5 MiB."""
    workspace = await _opened(room, agent_user_id)
    return await asyncio.to_thread(operations.read_file, workspace.root, path)


@router.put("/agents/{agent_user_id}/workspace/file", response_model=operations.Saved)
async def save_file(
    request: Request,
    room: _Room,
    room_id: str,
    agent_user_id: str,
    body: _Save,
) -> operations.Saved | JSONResponse:
    """Save a file; given ``etag``, only while the file has it, else 409 with the file's current etag.

    With ``create``, only where nothing is, else 409.
    """
    try:
        payload = body.content.encode() if body.encoding == "utf-8" else base64.b64decode(body.content, validate=True)
    except ValueError:  # Half of a surrogate pair, or text that is not base64.
        raise HTTPException(400, f"content is not valid {body.encoding}.") from None
    root = await _made_root(request, room, room_id, agent_user_id)
    if body.create:
        return await asyncio.to_thread(operations.create_file, root, body.path, payload)
    try:
        return await asyncio.to_thread(operations.save_file, root, body.path, payload, body.etag)
    except operations.StaleEtagError as stale:
        return JSONResponse({"detail": str(stale), "etag": stale.etag}, status_code=409)


@router.post("/agents/{agent_user_id}/workspace/folder", status_code=204)
async def make_folder(request: Request, room: _Room, room_id: str, agent_user_id: str, body: _Path) -> None:
    """Make a folder and the folders above it; 409 when something is already there."""
    root = await _made_root(request, room, room_id, agent_user_id)
    await asyncio.to_thread(operations.make_folder, root, body.path)


@router.post("/agents/{agent_user_id}/workspace/move", status_code=204)
async def move(room: _Room, agent_user_id: str, body: _Move) -> None:
    """Move or rename a file, folder or link; 409 when ``to`` is taken."""
    workspace = await _writable(room, agent_user_id)
    await asyncio.to_thread(operations.move, workspace.root, body.path, body.to)


@router.delete("/agents/{agent_user_id}/workspace/entry", status_code=204)
async def delete(room: _Room, agent_user_id: str, path: str) -> None:
    """Delete a file, a link, or a folder with everything in it."""
    workspace = await _writable(room, agent_user_id)
    await asyncio.to_thread(operations.delete, workspace.root, path)


@router.websocket("/agents/{agent_user_id}/workspace/changes")
async def follow_changes(websocket: WebSocket, room_id: str, agent_user_id: str) -> None:
    """Send ``{"type": "ready"}`` once following, then ``{"type": "changed", "paths": [...]}`` for each batch of changes.

    It closes with 1008 once the workspace is no longer open to the caller or moved, and 1013 while that can't be told.
    """
    async with client_stream(websocket, room_id) as user:
        try:
            workspace = await _opened(await stream_room_reader(websocket, user, room_id), agent_user_id)
            async with workspace_changes(workspace.root) as next_changes:
                await websocket.send_json({"type": "ready"})
                await agent_streams.until_first(
                    _send_changes(websocket, next_changes),
                    _until_closed(websocket),
                    _recheck(agent_streams.rechecked(websocket, user, room_id, agent_user_id), workspace),
                )
        except HTTPException as error:
            await agent_streams.close_answering(websocket, error.status_code, error.detail)
        except WebSocketDisconnect:
            pass


async def _send_changes(websocket: WebSocket, next_changes: Callable[[], Awaitable[list[str]]]) -> None:
    while True:
        paths = await next_changes()
        # A client that stopped reading blocks the send, and with it the follower's own overflow answer.
        try:
            async with asyncio.timeout(agent_streams.RECHECK_SECONDS):
                await websocket.send_json({"type": "changed", "paths": paths})
        except TimeoutError:
            raise HTTPException(503, "The client stopped reading changes; connect again.") from None


async def _until_closed(websocket: WebSocket) -> None:
    """Return once the client closes the stream; what it sends is ignored."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _recheck(rechecked: AsyncIterator[AgentWorkspace], workspace: AgentWorkspace) -> None:
    async for current in rechecked:
        if _with_files(current).root != workspace.root:
            raise HTTPException(403, "The agent's workspace moved for you.")
