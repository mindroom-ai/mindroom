"""Knowledge base management API."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.manager import (
    git_checkout_present,
    include_semantic_knowledge_relative_path,
)
from mindroom.knowledge.manager import (
    list_git_tracked_knowledge_files as list_git_tracked_managed_knowledge_files,
)
from mindroom.knowledge.manager import (
    list_knowledge_files as list_managed_knowledge_files,
)
from mindroom.knowledge.redaction import redact_credentials_in_text, redact_url_credentials
from mindroom.knowledge.refresh_runner import (
    is_refresh_active_for_binding,
    knowledge_binding_mutation_lock,
    refresh_knowledge_binding,
)
from mindroom.knowledge.status import (
    KnowledgeIndexStatus,
    get_knowledge_index_status,
    mark_knowledge_source_changed_async,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
logger = get_logger(__name__)

_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_DASHBOARD_GIT_FILE_LIST_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class _FileListInfo:
    files: list[dict[str, Any]]
    total_size: int
    degraded: bool = False
    error: str | None = None


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

    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path is outside the knowledge folder") from exc
    return resolved


async def _list_file_info(
    config: Config,
    base_id: str,
    root: Path,
    file_paths: list[Path] | None = None,
) -> _FileListInfo:
    files: list[dict[str, Any]] = []
    total_size = 0
    resolved_root = root.resolve()

    if not resolved_root.is_dir():
        return _FileListInfo(files=files, total_size=total_size)

    managed_paths, error = await _list_managed_file_paths(config, base_id, resolved_root)
    if error is not None:
        return _FileListInfo(files=[], total_size=0, degraded=True, error=error)
    paths = (
        sorted(managed_paths) if file_paths is None else sorted(path for path in file_paths if path in managed_paths)
    )
    for file_path in paths:
        try:
            stat = file_path.stat()
            relative_path = file_path.relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            continue
        total_size += stat.st_size
        file_type = file_path.suffix.lstrip(".").lower() if file_path.suffix else "file"
        files.append(
            {
                "name": file_path.name,
                "path": relative_path,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "type": file_type,
            },
        )

    return _FileListInfo(files=files, total_size=total_size)


async def _list_managed_file_paths(config: Config, base_id: str, root: Path) -> tuple[set[Path], str | None]:
    base_config = config.knowledge_bases[base_id]
    if base_config.git is None:
        return set(await asyncio.to_thread(list_managed_knowledge_files, config, base_id, root)), None
    try:
        paths = await asyncio.to_thread(
            list_git_tracked_managed_knowledge_files,
            config,
            base_id,
            root,
            timeout_seconds=_DASHBOARD_GIT_FILE_LIST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        error = redact_credentials_in_text(str(exc)) or "Git file listing timed out"
        logger.warning("Could not list Git-backed knowledge files", base_id=base_id, error=error)
        return set(), error
    return set(paths), None


def _request_refresh_scheduler(request: Request) -> KnowledgeRefreshScheduler | None:
    try:
        refresh_scheduler = request.app.state.knowledge_refresh_scheduler
    except AttributeError:
        return None
    return refresh_scheduler


def _schedule_refresh(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
) -> None:
    refresh_scheduler = _request_refresh_scheduler(request)
    if refresh_scheduler is None:
        return
    refresh_scheduler.schedule_refresh(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )


def _schedule_refreshes(
    config: Config,
    base_ids: tuple[str, ...],
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
) -> None:
    for base_id in dict.fromkeys(base_ids):
        _schedule_refresh(config, base_id, runtime_paths, request=request)


def _same_source_base_ids(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
) -> tuple[str, ...]:
    source_root = _knowledge_root(config, base_id, runtime_paths).resolve()
    base_ids = [base_id]
    for candidate_id in config.knowledge_bases:
        if candidate_id == base_id:
            continue
        if _knowledge_root(config, candidate_id, runtime_paths).resolve() == source_root:
            base_ids.append(candidate_id)
    return tuple(base_ids)


async def _mark_source_changed_after_committed_mutation(
    base_id: str,
    *,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    reason: str,
) -> tuple[tuple[str, ...], bool]:
    source_changed_task = asyncio.create_task(
        mark_knowledge_source_changed_async(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            reason=reason,
        ),
    )
    try:
        return await asyncio.shield(source_changed_task), False
    except asyncio.CancelledError:
        return await source_changed_task, True


async def _mark_committed_mutation_and_schedule_refresh(
    base_id: str,
    *,
    config: Config,
    runtime_paths: constants.RuntimePaths,
    request: Request,
    reason: str,
) -> bool:
    try:
        affected_base_ids, cancelled_after_source_changed = await _mark_source_changed_after_committed_mutation(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            reason=reason,
        )
    except Exception:
        _schedule_refreshes(
            config,
            _same_source_base_ids(config, base_id, runtime_paths),
            runtime_paths,
            request=request,
        )
        raise
    _schedule_refreshes(config, (base_id, *affected_base_ids), runtime_paths, request=request)
    return cancelled_after_source_changed


def _index_status_sync(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
) -> KnowledgeIndexStatus:
    try:
        return get_knowledge_index_status(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            create=False,
        )
    except Exception:
        logger.warning("Could not resolve published knowledge index state", base_id=base_id, exc_info=True)
        return KnowledgeIndexStatus()


async def _index_status(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
) -> KnowledgeIndexStatus:
    return await asyncio.to_thread(_index_status_sync, config, base_id, runtime_paths)


def _redacted_last_error(value: str | None) -> str | None:
    if value is None:
        return None
    return redact_credentials_in_text(value)


def _is_refreshing(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
) -> bool:
    refresh_scheduler = _request_refresh_scheduler(request)
    if refresh_scheduler is not None:
        return refresh_scheduler.is_refreshing(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
        )
    return is_refresh_active_for_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )


async def _git_status(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    *,
    request: Request,
    index_status: KnowledgeIndexStatus | None = None,
) -> dict[str, Any] | None:
    git_config = config.knowledge_bases[base_id].git
    if git_config is None:
        return None
    root = _knowledge_root(config, base_id, runtime_paths)
    if index_status is None:
        index_status = _index_status_sync(config, base_id, runtime_paths)
    repo_present = await asyncio.to_thread(
        git_checkout_present,
        root,
        timeout_seconds=_DASHBOARD_GIT_FILE_LIST_TIMEOUT_SECONDS,
    )
    return {
        "repo_url": redact_url_credentials(git_config.repo_url),
        "branch": git_config.branch,
        "lfs": git_config.lfs,
        "syncing": _is_refreshing(config, base_id, runtime_paths, request=request),
        "repo_present": repo_present,
        "initial_sync_complete": index_status.initial_sync_complete,
        "last_successful_sync_at": index_status.last_published_at,
        "last_successful_commit": index_status.published_revision,
        "last_error": _redacted_last_error(index_status.last_error),
    }


def _path_overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _git_backed_bases_for_target(
    config: Config,
    target: Path,
    runtime_paths: constants.RuntimePaths,
) -> tuple[str, ...]:
    resolved_target = target.resolve()
    git_base_ids: list[str] = []
    for candidate_id, candidate_config in config.knowledge_bases.items():
        if candidate_config.git is None:
            continue
        candidate_root = constants.resolve_config_relative_path(candidate_config.path, runtime_paths).resolve()
        if _path_overlaps(resolved_target, candidate_root):
            git_base_ids.append(candidate_id)
    return tuple(git_base_ids)


def _reject_git_file_mutation(
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    target: Path,
) -> None:
    git_base_ids = _git_backed_bases_for_target(config, target, runtime_paths)
    if not git_base_ids:
        return
    if base_id in git_base_ids:
        detail = (
            f"Knowledge base '{base_id}' is Git-backed. "
            "Update the configured repository and reindex instead of mutating files through this API."
        )
    else:
        joined_base_ids = ", ".join(sorted(git_base_ids))
        detail = (
            f"Knowledge base '{base_id}' shares its source path with Git-backed knowledge base(s): "
            f"{joined_base_ids}. Update the configured repository and reindex instead of mutating files through this API."
        )
    raise HTTPException(
        status_code=409,
        detail=detail,
    )


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


def _reject_non_file_upload_destination(destination: Path, relative_path: str) -> None:
    if destination.exists() and not destination.is_file():
        raise HTTPException(
            status_code=409,
            detail=f"Upload destination '{relative_path}' already exists and is not a regular file",
        )


def _reject_unmanaged_knowledge_file_path(config: Config, base_id: str, relative_path: str) -> None:
    if include_semantic_knowledge_relative_path(config, base_id, relative_path):
        return
    raise HTTPException(
        status_code=415,
        detail=(
            f"File '{relative_path}' is not supported by knowledge base '{base_id}' "
            "because it is excluded by the managed file filters"
        ),
    )


def _reject_duplicate_upload_destination(relative_path: str) -> None:
    raise HTTPException(
        status_code=409,
        detail=f"Upload batch contains duplicate destination '{relative_path}'",
    )


def _upload_temp_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload.tmp")


@dataclass(frozen=True, slots=True)
class _UploadTarget:
    upload: UploadFile
    destination: Path
    filename: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class _StagedUpload:
    temp_path: Path
    destination: Path
    relative_path: str


async def _stage_upload(upload: UploadFile, destination: Path, filename: str, relative_path: str) -> _StagedUpload:
    _validate_upload_size_hint(upload, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _upload_temp_path(destination)
    try:
        await _stream_upload_to_destination(upload, temp_path, filename)
    except (asyncio.CancelledError, Exception):
        temp_path.unlink(missing_ok=True)
        raise
    return _StagedUpload(temp_path=temp_path, destination=destination, relative_path=relative_path)


async def _write_uploads(
    files: list[UploadFile],
    *,
    config: Config,
    base_id: str,
    runtime_paths: constants.RuntimePaths,
    root: Path,
) -> list[str]:
    try:
        upload_targets: list[_UploadTarget] = []
        seen_relative_paths: set[str] = set()
        resolved_root = root.resolve()
        for upload in files:
            filename = Path(upload.filename or "").name
            if not filename:
                continue

            destination = _resolve_within_root(root, filename)
            _reject_git_file_mutation(config, base_id, runtime_paths, destination)
            relative_path = destination.relative_to(resolved_root).as_posix()
            _reject_unmanaged_knowledge_file_path(config, base_id, relative_path)
            if relative_path in seen_relative_paths:
                _reject_duplicate_upload_destination(relative_path)
            seen_relative_paths.add(relative_path)
            _reject_non_file_upload_destination(destination, relative_path)
            upload_targets.append(
                _UploadTarget(
                    upload=upload,
                    destination=destination,
                    filename=filename,
                    relative_path=relative_path,
                ),
            )

        staged_uploads: list[_StagedUpload] = []
        try:
            staged_uploads = [
                await _stage_upload(
                    target.upload,
                    target.destination,
                    target.filename,
                    target.relative_path,
                )
                for target in upload_targets
            ]
            for staged_upload in staged_uploads:
                staged_upload.temp_path.replace(staged_upload.destination)
        except (asyncio.CancelledError, Exception):
            for staged_upload in staged_uploads:
                staged_upload.temp_path.unlink(missing_ok=True)
            raise
        return [staged_upload.relative_path for staged_upload in staged_uploads]
    finally:
        for upload in files:
            await upload.close()


@router.get("/bases")
async def list_knowledge_bases(request: Request) -> dict[str, Any]:
    """List all configured knowledge bases with status summaries."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)

    bases: list[dict[str, Any]] = []
    for base_id in sorted(config.knowledge_bases):
        base_config = config.knowledge_bases[base_id]
        root = _knowledge_root(config, base_id, runtime_paths)
        file_info = await _list_file_info(config, base_id, root)
        index_status = await _index_status(config, base_id, runtime_paths)
        git_status = await _git_status(
            config,
            base_id,
            runtime_paths,
            request=request,
            index_status=index_status,
        )
        refreshing = _is_refreshing(config, base_id, runtime_paths, request=request)

        base_entry = {
            "name": base_id,
            "path": str(root),
            "watch": base_config.watch,
            "file_count": len(file_info.files),
            "indexed_count": index_status.indexed_count,
            "refreshing": refreshing,
            "refresh_state": index_status.refresh_state,
            "file_listing_degraded": file_info.degraded,
        }
        if index_status.last_error is not None:
            base_entry["last_error"] = _redacted_last_error(index_status.last_error)
        if file_info.error is not None:
            base_entry["file_listing_error"] = file_info.error
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
    file_info = await _list_file_info(config, base_id, root)

    return {
        "base_id": base_id,
        "files": file_info.files,
        "total_size": file_info.total_size,
        "file_count": len(file_info.files),
        "file_listing_degraded": file_info.degraded,
        "file_listing_error": file_info.error,
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
    uploaded: list[str] = []

    async with knowledge_binding_mutation_lock(base_id, config=config, runtime_paths=runtime_paths):
        root = _knowledge_root(config, base_id, runtime_paths)

        uploaded = await _write_uploads(
            files,
            config=config,
            base_id=base_id,
            runtime_paths=runtime_paths,
            root=root,
        )

        if not uploaded:
            return {
                "base_id": base_id,
                "uploaded": [],
                "count": 0,
            }

        cancelled_after_source_changed = await _mark_committed_mutation_and_schedule_refresh(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            request=request,
            reason="dashboard_upload",
        )
        if cancelled_after_source_changed:
            raise asyncio.CancelledError

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
    root = _knowledge_root(config, base_id, runtime_paths)
    target = _resolve_within_root(root, path)
    _reject_git_file_mutation(config, base_id, runtime_paths, target)

    async with knowledge_binding_mutation_lock(base_id, config=config, runtime_paths=runtime_paths):
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Knowledge file not found")

        relative_path = target.relative_to(root.resolve()).as_posix()
        _reject_unmanaged_knowledge_file_path(config, base_id, relative_path)
        target.unlink()
        cancelled_after_source_changed = await _mark_committed_mutation_and_schedule_refresh(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            request=request,
            reason="dashboard_delete",
        )
        if cancelled_after_source_changed:
            raise asyncio.CancelledError

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
    index_status = await _index_status(config, base_id, runtime_paths)
    file_info = await _list_file_info(config, base_id, root)
    git_status = await _git_status(
        config,
        base_id,
        runtime_paths,
        request=request,
        index_status=index_status,
    )
    refreshing = _is_refreshing(config, base_id, runtime_paths, request=request)

    payload = {
        "base_id": base_id,
        "folder_path": str(root),
        "watch": config.knowledge_bases[base_id].watch,
        "file_count": len(file_info.files),
        "indexed_count": index_status.indexed_count,
        "refreshing": refreshing,
        "refresh_state": index_status.refresh_state,
        "last_error": _redacted_last_error(index_status.last_error),
        "file_listing_degraded": file_info.degraded,
        "file_listing_error": file_info.error,
    }
    if git_status is not None:
        payload["git"] = git_status
    return payload


@router.post("/bases/{base_id}/reindex")
async def reindex_knowledge(base_id: str, request: Request) -> dict[str, Any]:
    """Force reindexing of all files in one knowledge base folder."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    _ensure_base_exists(config, base_id)

    try:
        refresh_scheduler = _request_refresh_scheduler(request)
        if refresh_scheduler is not None:
            result = await refresh_scheduler.refresh_now(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                force_reindex=True,
            )
        else:
            result = await refresh_knowledge_binding(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                force_reindex=True,
            )
    except Exception as exc:
        index_status = _index_status_sync(config, base_id, runtime_paths)
        availability = (
            index_status.availability
            if index_status.persisted_index_status is not None
            else KnowledgeAvailability.REFRESH_FAILED
        )
        last_error = index_status.last_error if index_status.last_error is not None else str(exc)
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "base_id": base_id,
                "indexed_count": index_status.indexed_count,
                "availability": availability.value,
                "last_error": _redacted_last_error(last_error),
            },
        ) from exc
    if not (result.index_published and result.availability is KnowledgeAvailability.READY):
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "base_id": base_id,
                "indexed_count": result.indexed_count,
                "availability": result.availability.value,
                "last_error": _redacted_last_error(result.last_error),
            },
        )
    return {
        "success": True,
        "base_id": base_id,
        "indexed_count": result.indexed_count,
    }
