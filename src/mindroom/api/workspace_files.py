"""Chat clients' routes to the workspaces of a room's agents: list them, and read and change their files.

Every route signs its caller in with a client session and checks them through ``RoomReader`` on each request, so a
workspace opens only while the room and the agent's access rules let the caller use that agent there. The agent's user
ID names the workspace; its folder is the one a turn of that agent for this caller in this room uses, never a path
from the request, and file paths are relative to it (see ``mindroom.workspace_files.operations``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.plugin_http import AgentWorkspace, RoomReader, require_room_reader
from mindroom.room_agent_access import room_execution_identity
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.workspace_files import operations

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter(prefix="/api/workspaces/v1/rooms/{room_id}", tags=["workspace-files"])

_Room = Annotated[RoomReader, Depends(require_room_reader)]


@dataclass(frozen=True)
class OpenWorkspace:
    """A workspace the caller may open in the room, named by its agent."""

    agent_user_id: str
    display_name: str
    private: bool
    """Whether the agent is private, so this caller's workspace is their own."""
    files: Literal["read", "write"]


class _Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    etag: str | None = None


class _Path(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class _Move(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    to: str


async def _opened(room: RoomReader, agent_user_id: str) -> AgentWorkspace:
    workspace = await room.agent(agent_user_id)
    if workspace.files == "none":
        raise HTTPException(403, "This agent's tools reach no files.")
    return workspace


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


@router.get("")
async def list_workspaces(room: _Room) -> dict[str, list[OpenWorkspace]]:
    """Return the workspaces the caller may open in the room, in config order, with whether they may change files."""
    return {
        "workspaces": [
            OpenWorkspace(workspace.user_id, workspace.display_name, workspace.private, workspace.files)
            for workspace in await room.agents()
            if workspace.files != "none"
        ],
    }


@router.get("/{agent_user_id}/folder")
async def list_folder(room: _Room, agent_user_id: str, path: str = "") -> operations.Folder:
    """Return one folder's entries, folders first; the root lists empty before the agent's first turn."""
    workspace = await _opened(room, agent_user_id)
    return await asyncio.to_thread(operations.list_folder, workspace.root, path)


@router.get("/{agent_user_id}/file")
async def read_file(room: _Room, agent_user_id: str, path: str) -> operations.FileContent:
    """Return a file's content with its etag, answering 413 for one over 5 MiB."""
    workspace = await _opened(room, agent_user_id)
    return await asyncio.to_thread(operations.read_file, workspace.root, path)


@router.put("/{agent_user_id}/file", response_model=operations.Saved)
async def save_file(
    request: Request,
    room: _Room,
    room_id: str,
    agent_user_id: str,
    body: _Save,
) -> operations.Saved | JSONResponse:
    """Save a file; given ``etag``, only while the file has it, else 409 with the file's current etag."""
    try:
        payload = body.content.encode() if body.encoding == "utf-8" else base64.b64decode(body.content, validate=True)
    except UnicodeEncodeError:
        raise HTTPException(400, "content is not valid Unicode text.") from None
    except binascii.Error:
        raise HTTPException(400, "content is not valid base64.") from None
    if len(payload) > operations.MAX_FILE_BYTES:
        raise HTTPException(413, f"The file is over {operations.MAX_FILE_BYTES >> 20} MiB.")
    root = await _made_root(request, room, room_id, agent_user_id)
    try:
        return await asyncio.to_thread(operations.save_file, root, body.path, payload, body.etag)
    except operations.StaleEtagError as stale:
        return JSONResponse({"detail": str(stale), "etag": stale.etag}, status_code=409)


@router.post("/{agent_user_id}/folder", status_code=204)
async def make_folder(request: Request, room: _Room, room_id: str, agent_user_id: str, body: _Path) -> None:
    """Make a folder and the folders above it; 409 when something is already there."""
    root = await _made_root(request, room, room_id, agent_user_id)
    await asyncio.to_thread(operations.make_folder, root, body.path)


@router.post("/{agent_user_id}/move", status_code=204)
async def move(room: _Room, agent_user_id: str, body: _Move) -> None:
    """Move or rename a file, folder or link; 409 when ``to`` is taken."""
    workspace = await _writable(room, agent_user_id)
    await asyncio.to_thread(operations.move, workspace.root, body.path, body.to)


@router.delete("/{agent_user_id}/entry", status_code=204)
async def delete(room: _Room, agent_user_id: str, path: str) -> None:
    """Delete a file, a link, or a folder with everything in it."""
    workspace = await _writable(room, agent_user_id)
    await asyncio.to_thread(operations.delete, workspace.root, path)
