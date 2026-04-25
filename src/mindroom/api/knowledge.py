"""Knowledge base management API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import unquote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.knowledge import (
    KnowledgeAvailability,
    PublishedIndexingState,
    get_published_snapshot,
    load_published_indexing_state,
    redact_url_credentials,
    refresh_knowledge_binding,
    remove_source_path_from_published_snapshots,
    resolve_snapshot_key,
    snapshot_indexed_count,
    snapshot_metadata_path,
)
from mindroom.knowledge import (
    list_knowledge_files as list_managed_knowledge_files,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.knowledge.refresh_owner import KnowledgeRefreshOwner

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


def _ensure_base_exists(config: Config, base_id: str) -> None:
    if base_id not in config.knowledge_bases:
        raise HTTPException(status_code=404, detail=f"Knowledge base '{base_id}' not found")


def _knowledge_root(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    create: bool = False,
) -> Path:
    _ensure_base_exists(config, base_id)
    root = constants.resolve_config_relative_path(config.knowledge_bases[base_id].path, runtime_paths)
    if create:
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


def _list_file_info(
    config: Config,
    base_id: str,
    root: Path,
    file_paths: list[Path] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    total_size = 0

    if not root.is_dir():
        return files, total_size

    managed_paths = set(list_managed_knowledge_files(config, base_id, root))
    paths = (
        sorted(managed_paths) if file_paths is None else sorted(path for path in file_paths if path in managed_paths)
    )
    for file_path in paths:
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


def _request_refresh_owner(request: Request) -> KnowledgeRefreshOwner | None:
    try:
        owner = request.app.state.knowledge_refresh_owner
    except AttributeError:
        return None
    return owner


def _schedule_refresh(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
) -> None:
    owner = _request_refresh_owner(request)
    if owner is None:
        return
    owner.schedule_refresh(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )


def _snapshot_status(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
) -> tuple[bool, int]:
    lookup = get_published_snapshot(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if lookup.snapshot is None:
        return False, 0
    return lookup.availability is not KnowledgeAvailability.INITIALIZING, snapshot_indexed_count(lookup.snapshot)


def _snapshot_state(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
) -> PublishedIndexingState | None:
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        create=False,
    )
    return load_published_indexing_state(snapshot_metadata_path(key))


def _git_status(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
) -> dict[str, Any] | None:
    git_config = config.knowledge_bases[base_id].git
    if git_config is None:
        return None
    root = _knowledge_root(config, base_id, runtime_paths)
    state = _snapshot_state(config, base_id, runtime_paths)
    owner = _request_refresh_owner(request)
    return {
        "repo_url": redact_url_credentials(git_config.repo_url),
        "branch": git_config.branch,
        "lfs": git_config.lfs,
        "startup_behavior": git_config.startup_behavior,
        "syncing": (
            owner.is_refreshing(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
            )
            if owner is not None
            else None
        ),
        "repo_present": (root / ".git").is_dir(),
        "initial_sync_complete": (
            state is not None and state.status == "complete" and state.published_revision is not None
        ),
        "last_successful_sync_at": state.last_published_at if state is not None else None,
        "last_successful_commit": state.published_revision if state is not None else None,
        "last_error": state.last_error if state is not None else None,
    }


def _reject_git_file_mutation(config: Config, base_id: str) -> None:
    if config.knowledge_bases[base_id].git is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Knowledge base '{base_id}' is Git-backed. "
            "Update the configured repository and reindex instead of mutating files through this API."
        ),
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


@router.get("/bases")
async def list_knowledge_bases(request: Request) -> dict[str, Any]:
    """List all configured knowledge bases with status summaries."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)

    bases: list[dict[str, Any]] = []
    for base_id in sorted(config.knowledge_bases):
        base_config = config.knowledge_bases[base_id]
        root = _knowledge_root(config, base_id, runtime_paths)
        file_count = len(_list_file_info(config, base_id, root)[0])
        snapshot_available, indexed_count = _snapshot_status(config, base_id, runtime_paths)
        git_status = _git_status(config, base_id, runtime_paths, request=request)

        base_entry = {
            "name": base_id,
            "path": str(root),
            "watch": base_config.watch,
            "file_count": file_count,
            "indexed_count": indexed_count,
            "manager_available": snapshot_available,
        }
        if git_status is not None:
            base_entry["git"] = git_status
        bases.append(base_entry)

    return {
        "bases": bases,
        "count": len(bases),
    }


@router.get("/bases/{base_id}/files")
async def list_knowledge_files(base_id: str, request: Request) -> dict[str, Any]:
    """List all managed files currently present in one knowledge base folder."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    root = _knowledge_root(config, base_id, runtime_paths)
    snapshot_available, _indexed_count = _snapshot_status(config, base_id, runtime_paths)
    files, total_size = _list_file_info(config, base_id, root)

    return {
        "base_id": base_id,
        "files": files,
        "total_size": total_size,
        "file_count": len(files),
        "manager_available": snapshot_available,
    }


@router.post("/bases/{base_id}/upload")
async def upload_knowledge_files(
    base_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
) -> dict[str, Any]:
    """Upload one or more files into a knowledge base folder."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    _ensure_base_exists(config, base_id)
    _reject_git_file_mutation(config, base_id)
    root = _knowledge_root(config, base_id, runtime_paths, create=True)

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

    _schedule_refresh(config, base_id, runtime_paths, request=request)

    return {
        "base_id": base_id,
        "uploaded": uploaded,
        "count": len(uploaded),
    }


@router.delete("/bases/{base_id}/files/{path:path}")
async def delete_knowledge_file(base_id: str, path: str, request: Request) -> dict[str, Any]:
    """Delete one knowledge file from disk and from the vector index."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    _ensure_base_exists(config, base_id)
    _reject_git_file_mutation(config, base_id)
    root = _knowledge_root(config, base_id, runtime_paths)
    decoded_path = unquote(path)
    target = _resolve_within_root(root, decoded_path)

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Knowledge file not found")

    relative_path = target.relative_to(root).as_posix()
    target.unlink()
    remove_source_path_from_published_snapshots(
        base_id,
        relative_path,
        config=config,
        runtime_paths=runtime_paths,
    )

    _schedule_refresh(config, base_id, runtime_paths, request=request)

    return {
        "success": True,
        "base_id": base_id,
        "path": relative_path,
    }


@router.get("/bases/{base_id}/status")
async def knowledge_status(base_id: str, request: Request) -> dict[str, Any]:
    """Return current indexing status for one knowledge base."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    root = _knowledge_root(config, base_id, runtime_paths)
    snapshot_available, indexed_count = _snapshot_status(config, base_id, runtime_paths)
    state = _snapshot_state(config, base_id, runtime_paths)
    file_count = len(_list_file_info(config, base_id, root)[0])
    git_status = _git_status(config, base_id, runtime_paths, request=request)

    payload = {
        "base_id": base_id,
        "folder_path": str(root),
        "watch": config.knowledge_bases[base_id].watch,
        "file_count": file_count,
        "indexed_count": indexed_count,
        "manager_available": snapshot_available,
        "last_error": state.last_error if state is not None else None,
    }
    if git_status is not None:
        payload["git"] = git_status
    return payload


@router.post("/bases/{base_id}/reindex")
async def reindex_knowledge(base_id: str, request: Request) -> dict[str, Any]:
    """Force reindexing of all files in one knowledge base folder."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    _ensure_base_exists(config, base_id)

    result = await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths)
    if not result.published:
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "base_id": base_id,
                "indexed_count": result.indexed_count,
                "availability": result.availability.value,
                "last_error": result.last_error,
            },
        )
    return {
        "success": True,
        "base_id": base_id,
        "indexed_count": result.indexed_count,
    }
