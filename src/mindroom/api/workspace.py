"""Workspace markdown file management API."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from mindroom.config import Config
from mindroom.constants import STORAGE_PATH_OBJ
from mindroom.workspace import (
    AGENTS_FILENAME,
    MEMORY_FILENAME,
    SOUL_FILENAME,
    ensure_workspace,
    get_agent_workspace_path,
)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

_ALLOWED_ROOT_FILES = {SOUL_FILENAME, AGENTS_FILENAME, MEMORY_FILENAME}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MEMORY_FILE_PATTERN = re.compile(r"^memory(?:/[A-Za-z0-9._-]+)*/[A-Za-z0-9._-]+\.md$")


class WorkspaceFileUpdate(BaseModel):
    """Payload for workspace file updates."""

    content: str = Field(default="", description="New markdown file content")


def _workspace_root(config: Config, agent_name: str) -> Path:
    if agent_name not in config.agents:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    root = get_agent_workspace_path(agent_name, STORAGE_PATH_OBJ).resolve()
    if not root.exists():
        ensure_workspace(agent_name, STORAGE_PATH_OBJ, config)
        return root

    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "rooms").mkdir(parents=True, exist_ok=True)
    return root


def _resolve_within_root(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path is outside the workspace folder") from exc
    return resolved


def _validate_filename(filename: str) -> str:
    decoded = unquote(filename).strip("/")
    path_segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in {".", ".."} for segment in path_segments):
        raise HTTPException(status_code=422, detail="Filename is not allowed")

    if decoded in _ALLOWED_ROOT_FILES:
        return decoded
    if _MEMORY_FILE_PATTERN.fullmatch(decoded):
        return decoded
    raise HTTPException(status_code=422, detail="Filename is not allowed")


def _etag_for_content(content: str) -> str:
    digest = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f'"{digest}"'


def _normalize_etag(etag: str) -> str:
    normalized = etag.strip()
    normalized = normalized.removeprefix("W/")
    if not normalized.startswith('"'):
        normalized = f'"{normalized}"'
    return normalized


def _file_metadata(path: Path, root: Path, agent_name: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "filename": path.relative_to(root).as_posix(),
        "size_bytes": stat.st_size,
        "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        "agent_name": agent_name,
    }


def _workspace_file_entries(root: Path, agent_name: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for root_filename in sorted(_ALLOWED_ROOT_FILES):
        target = root / root_filename
        if target.exists() and target.is_file():
            entries.append(_file_metadata(target, root, agent_name))

    memory_dir = root / "memory"
    if memory_dir.exists():
        entries.extend(
            _file_metadata(target, root, agent_name) for target in sorted(memory_dir.rglob("*.md")) if target.is_file()
        )

    entries.sort(key=lambda entry: str(entry["filename"]))
    return entries


@router.get("/{agent_name}/files")
async def list_workspace_files(agent_name: str) -> dict[str, Any]:
    """List allowed workspace files for an agent."""
    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    files = _workspace_file_entries(root, agent_name)
    return {
        "agent_name": agent_name,
        "files": files,
        "count": len(files),
    }


@router.get("/{agent_name}/file/{filename:path}")
async def read_workspace_file(agent_name: str, filename: str, response: Response) -> dict[str, Any]:
    """Read one workspace file and return content + metadata."""
    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    allowed_filename = _validate_filename(filename)
    target = _resolve_within_root(root, allowed_filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Workspace file not found")

    content = target.read_text(encoding="utf-8")
    response.headers["ETag"] = _etag_for_content(content)
    return {
        **_file_metadata(target, root, agent_name),
        "content": content,
    }


@router.put("/{agent_name}/file/{filename:path}")
async def update_workspace_file(
    agent_name: str,
    filename: str,
    payload: WorkspaceFileUpdate,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Update one workspace file with optimistic concurrency checks."""
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match header is required")

    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    allowed_filename = _validate_filename(filename)
    target = _resolve_within_root(root, allowed_filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Workspace file not found")

    current_content = target.read_text(encoding="utf-8")
    current_etag = _etag_for_content(current_content)
    if _normalize_etag(if_match) != current_etag:
        raise HTTPException(status_code=409, detail="ETag mismatch")

    new_size = len(payload.content.encode("utf-8"))
    max_size = config.memory.workspace.max_file_size
    if new_size > max_size:
        raise HTTPException(status_code=400, detail=f"Content exceeds max file size ({max_size} bytes)")

    target.write_text(payload.content, encoding="utf-8")
    response.headers["ETag"] = _etag_for_content(payload.content)
    return {
        **_file_metadata(target, root, agent_name),
        "content": payload.content,
    }


@router.delete("/{agent_name}/file/{filename:path}")
async def delete_workspace_file(agent_name: str, filename: str) -> dict[str, Any]:
    """Delete one allowed workspace file."""
    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    allowed_filename = _validate_filename(filename)
    target = _resolve_within_root(root, allowed_filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Workspace file not found")

    relative_path = target.relative_to(root).as_posix()
    target.unlink()
    return {
        "success": True,
        "agent_name": agent_name,
        "filename": relative_path,
    }


@router.get("/{agent_name}/memory/daily")
async def list_daily_logs(agent_name: str) -> dict[str, Any]:
    """List all daily logs for one agent across scopes."""
    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    memory_dir = root / "memory"
    files = [
        _file_metadata(path, root, agent_name)
        for path in sorted(memory_dir.rglob("*.md"))
        if path.is_file() and _DATE_PATTERN.fullmatch(path.stem)
    ]
    return {
        "agent_name": agent_name,
        "files": files,
        "count": len(files),
    }


@router.get("/{agent_name}/memory/daily/{date}")
async def read_daily_logs_for_date(agent_name: str, date: str) -> dict[str, Any]:
    """Read all daily logs for one date across scopes."""
    if not _DATE_PATTERN.fullmatch(date):
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")

    config = Config.from_yaml()
    root = _workspace_root(config, agent_name)
    memory_dir = root / "memory"
    matches = [
        path for path in sorted(memory_dir.rglob(f"{date}.md")) if path.is_file() and _DATE_PATTERN.fullmatch(path.stem)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Daily log not found")

    entries = [
        {
            **_file_metadata(path, root, agent_name),
            "content": path.read_text(encoding="utf-8"),
        }
        for path in matches
    ]
    return {
        "agent_name": agent_name,
        "date": date,
        "entries": entries,
        "count": len(entries),
    }
