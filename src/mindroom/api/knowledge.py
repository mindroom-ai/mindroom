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

_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


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
    return await initialize_knowledge_manager(
        config,
        STORAGE_PATH_OBJ,
        start_watcher=False,
        reindex_on_create=False,
    )


def _rollback_uploaded_files(uploaded_paths: list[Path]) -> None:
    for uploaded_path in uploaded_paths:
        uploaded_path.unlink(missing_ok=True)


def _validate_upload_size_hint(upload: UploadFile, filename: str) -> None:
    if not upload.file.seekable():
        return

    current_position = upload.file.tell()
    upload.file.seek(0, 2)
    size_hint = upload.file.tell()
    upload.file.seek(current_position)

    if size_hint > _MAX_UPLOAD_BYTES:
        raise _upload_limit_error(filename)


def _upload_limit_error(filename: str) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"File '{filename}' exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit",
    )


def _ensure_within_upload_limit(bytes_written: int, filename: str) -> None:
    if bytes_written > _MAX_UPLOAD_BYTES:
        raise _upload_limit_error(filename)


async def _stream_upload_to_destination(upload: UploadFile, destination: Path, filename: str) -> None:
    bytes_written = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
            bytes_written += len(chunk)
            _ensure_within_upload_limit(bytes_written, filename)
            handle.write(chunk)


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
    uploaded_paths: list[Path] = []
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename:
            await upload.close()
            continue

        destination = _resolve_within_root(root, filename)

        try:
            _validate_upload_size_hint(upload, filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            await _stream_upload_to_destination(upload, destination, filename)
        except Exception:
            destination.unlink(missing_ok=True)
            _rollback_uploaded_files(uploaded_paths)
            raise
        finally:
            await upload.close()

        uploaded_paths.append(destination)
        uploaded.append(destination.relative_to(root).as_posix())

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
    manager = await _ensure_manager(config)
    if manager is not None:
        manager_status = manager.get_status()
        indexed_count = int(manager_status["indexed_count"])
        file_count = int(manager_status["file_count"])
    else:
        indexed_count = 0
        file_count = len(_list_file_info(root)[0])

    return {
        "enabled": config.knowledge.enabled,
        "folder_path": str(root),
        "file_count": file_count,
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
        manager = await initialize_knowledge_manager(
            config,
            STORAGE_PATH_OBJ,
            start_watcher=False,
            reindex_on_create=False,
        )

    if manager is None:
        raise HTTPException(status_code=500, detail="Knowledge manager is unavailable")

    indexed_count = await manager.reindex_all()
    return {
        "success": True,
        "indexed_count": indexed_count,
    }
