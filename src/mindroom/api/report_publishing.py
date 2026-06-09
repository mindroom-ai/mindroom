"""Public report publishing API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.api.report_headers import set_report_headers
from mindroom.report_publishing.store import ReportPublishingError, ReportPublishingStore

public_router = APIRouter(tags=["report-publishing"])


@public_router.get("/reports/public/{slug}", include_in_schema=False)
async def public_report(request: Request, slug: str) -> FileResponse:
    """Serve one active public HTML report from runtime storage."""
    runtime_paths = api_runtime_paths(request)
    store = ReportPublishingStore(runtime_paths.storage_root)
    try:
        report_path = store.public_report_html_path(slug)
    except ReportPublishingError as exc:
        raise HTTPException(status_code=404, detail="Public report was not found.") from exc

    response = FileResponse(report_path, media_type="text/html")
    set_report_headers(response, cache_control="no-store, max-age=0")
    return response
