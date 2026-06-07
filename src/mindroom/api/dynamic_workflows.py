"""Dynamic Workflow API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowStore

router = APIRouter(tags=["dynamic-workflows"])

_REPORT_CSP = (
    "default-src 'none'; "
    "img-src 'self' data: https:; "
    "style-src 'unsafe-inline'; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


@router.get("/reports/private/{run_id}", include_in_schema=False)
async def private_dynamic_workflow_report(request: Request, run_id: str) -> FileResponse:
    """Serve one private Dynamic Workflow HTML report from runtime storage."""
    store = DynamicWorkflowStore(api_runtime_paths(request).storage_root)
    try:
        report_path = store.find_private_report_html(run_id)
    except DynamicWorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    response = FileResponse(report_path, media_type="text/html")
    response.headers["Content-Security-Policy"] = _REPORT_CSP
    return response
