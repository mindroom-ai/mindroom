"""Dynamic Workflow API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowRun, DynamicWorkflowStore

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
async def legacy_private_dynamic_workflow_report(run_id: str) -> FileResponse:
    """Reject legacy unscoped Dynamic Workflow report URLs."""
    raise HTTPException(status_code=404, detail=f"Private report for run '{run_id}' was not found.")


@router.get("/reports/private/{scope}/{owner_key}/{workflow_id}/{run_id}", include_in_schema=False)
async def private_dynamic_workflow_report(
    request: Request,
    scope: str,
    owner_key: str,
    workflow_id: str,
    run_id: str,
) -> FileResponse:
    """Serve one private Dynamic Workflow HTML report from runtime storage."""
    store = DynamicWorkflowStore(api_runtime_paths(request).storage_root)
    try:
        run = store.get_workflow_run(
            scope=scope,
            owner_id=owner_key,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        _authorize_private_report_request(request, run)
        report_path = store.private_report_html_path(
            scope=scope,
            owner_key=owner_key,
            workflow_id=workflow_id,
            run_id=run_id,
        )
    except DynamicWorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    response = FileResponse(report_path, media_type="text/html")
    response.headers["Content-Security-Policy"] = _REPORT_CSP
    return response


def _authorize_private_report_request(request: Request, run: DynamicWorkflowRun) -> None:
    auth_user = request.scope.get("auth_user")
    if not isinstance(auth_user, dict):
        raise HTTPException(status_code=403, detail="Private Dynamic Workflow report access denied.")
    if auth_user.get("user_id") == "standalone":
        return
    if auth_user.get("matrix_user_id") == run.requested_by or auth_user.get("user_id") == run.requested_by:
        return
    raise HTTPException(status_code=403, detail="Private Dynamic Workflow report access denied.")
