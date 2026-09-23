"""Administrative thread exports through the running Matrix owners."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mindroom.api import config_lifecycle
from mindroom.thread_export.models import ThreadExportStats

router = APIRouter(prefix="/api/threads", tags=["threads"])


class ThreadExportRequest(BaseModel):
    """Export options plus the local installation the caller intended to use."""

    config_path: Path
    storage_root: Path
    output_dir: Path | None = None
    room_filter: str | None = None
    max_thread_roots: int = Field(default=2000, ge=1)
    include_invited_rooms: bool = True


@router.post("/export")
async def export_threads(body: ThreadExportRequest, request: Request) -> ThreadExportStats:
    """Borrow live clients; never open a journal or a Matrix login for this request."""
    paths = config_lifecycle.read_committed_config_and_runtime(request, lambda _config: None)[1]
    if body.config_path.resolve() != paths.config_path or body.storage_root.resolve() != paths.storage_root:
        raise HTTPException(
            status_code=409,
            detail="The running MindRoom instance uses a different config or storage path",
        )
    runner = config_lifecycle.app_state(request.app).thread_export_runner
    if runner is None:
        raise HTTPException(status_code=503, detail="Thread export requires a running MindRoom instance")
    try:
        return await runner.export_once(
            output_dir=body.output_dir,
            room_filter=body.room_filter,
            max_thread_roots=body.max_thread_roots,
            include_invited_rooms=body.include_invited_rooms,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
