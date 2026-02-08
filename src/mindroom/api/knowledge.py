"""Knowledge base management API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, File, HTTPException, UploadFile

from mindroom.config import Config
from mindroom.constants import STORAGE_PATH_OBJ
from mindroom.knowledge import (
    KnowledgeManager,
    get_knowledge_manager,
    initialize_knowledge_manager,
)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _knowledge_root(config: Config) -> Path:
    root = Path(config.knowledge.path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_within_root(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path is outside the knowledge folder") from exc
    return resolved


def _list_file_info(root: Path) -> tuple[list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    total_size = 0

    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        stat = file_path.stat()
        total_size += stat.st_size
        file_type = file_path.suffix.lstrip(".").lower() if file_path.suffix else "file"
        files.append(
            {
                "name": file_path.name,
                "path": file_path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "type": file_type,
            },
        )

    return files, total_size


async def _ensure_manager(config: Config) -> KnowledgeManager | None:
    if not config.knowledge.enabled:
        return None
    return await initialize_knowledge_manager(config, STORAGE_PATH_OBJ, start_watcher=False)


@router.get("/files")
async def list_knowledge_files() -> dict[str, Any]:
    """List all files currently present in the knowledge folder."""
    config = Config.from_yaml()
    root = _knowledge_root(config)
    files, total_size = _list_file_info(root)

    return {
        "files": files,
        "total_size": total_size,
        "file_count": len(files),
    }


@router.post("/upload")
async def upload_knowledge_files(files: Annotated[list[UploadFile], File(...)]) -> dict[str, Any]:
    """Upload one or more files into the knowledge folder."""
    config = Config.from_yaml()
    root = _knowledge_root(config)

    uploaded: list[str] = []
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename:
            await upload.close()
            continue

        destination = _resolve_within_root(root, filename)
        content = await upload.read()
        destination.write_bytes(content)
        uploaded.append(destination.relative_to(root).as_posix())
        await upload.close()

    if config.knowledge.enabled:
        manager = await _ensure_manager(config)
        if manager is not None:
            for relative_path in uploaded:
                await manager.index_file(relative_path, upsert=True)

    return {
        "uploaded": uploaded,
        "count": len(uploaded),
    }


@router.delete("/files/{path:path}")
async def delete_knowledge_file(path: str) -> dict[str, Any]:
    """Delete a knowledge file from disk and from the vector index."""
    config = Config.from_yaml()
    root = _knowledge_root(config)
    decoded_path = unquote(path)
    target = _resolve_within_root(root, decoded_path)

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Knowledge file not found")

    relative_path = target.relative_to(root).as_posix()
    target.unlink()

    if config.knowledge.enabled:
        manager = await _ensure_manager(config)
        if manager is not None:
            await manager.remove_file(relative_path)

    return {
        "success": True,
        "path": relative_path,
    }


@router.get("/status")
async def knowledge_status() -> dict[str, Any]:
    """Return current knowledge indexing status."""
    config = Config.from_yaml()
    root = _knowledge_root(config)
    files, _ = _list_file_info(root)

    indexed_count = 0
    manager = get_knowledge_manager()
    if config.knowledge.enabled:
        manager = await _ensure_manager(config)

    if manager is not None:
        indexed_count = manager.get_status()["indexed_count"]

    return {
        "enabled": config.knowledge.enabled,
        "folder_path": str(root),
        "file_count": len(files),
        "indexed_count": indexed_count,
    }


@router.post("/reindex")
async def reindex_knowledge() -> dict[str, Any]:
    """Force reindexing of all files in the knowledge folder."""
    config = Config.from_yaml()
    if not config.knowledge.enabled:
        raise HTTPException(status_code=400, detail="Knowledge base is disabled")

    manager = get_knowledge_manager()
    if manager is None or not manager.matches(config, STORAGE_PATH_OBJ):
        manager = await initialize_knowledge_manager(config, STORAGE_PATH_OBJ, start_watcher=False)

    if manager is None:
        raise HTTPException(status_code=500, detail="Knowledge manager is unavailable")

    indexed_count = await manager.reindex_all()
    return {
        "success": True,
        "indexed_count": indexed_count,
    }
