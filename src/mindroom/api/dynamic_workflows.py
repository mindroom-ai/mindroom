"""Dynamic Workflow API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from mindroom.api.auth_user import request_auth_user
from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.api.report_headers import set_report_headers
from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowStore
from mindroom.dynamic_workflows.validation import DynamicWorkflowError

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

router = APIRouter(tags=["dynamic-workflows"])


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
    runtime_paths = api_runtime_paths(request)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    try:
        run = store.get_workflow_run(
            scope=scope,
            owner_id=owner_key,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        _authorize_private_report_request(request, run, runtime_paths)
        report_path = store.private_report_html_path(
            scope=scope,
            owner_key=owner_key,
            workflow_id=workflow_id,
            run_id=run_id,
        )
    except DynamicWorkflowError as exc:
        raise HTTPException(status_code=404, detail="Private Dynamic Workflow report was not found.") from exc

    response = FileResponse(report_path, media_type="text/html")
    set_report_headers(response, cache_control="private, no-store, max-age=0")
    return response


def _authorize_private_report_request(
    request: Request,
    run: DynamicWorkflowRun,
    runtime_paths: RuntimePaths,
) -> None:
    """Admit the login that requested the run: as the Matrix user it acts as, or as an identity it carries."""
    auth_user = request_auth_user(request)
    # A gateway's user-ID claim can itself be the Matrix ID.
    if auth_user is None or run.requested_by not in {
        auth_user.requester_id(runtime_paths),
        auth_user.matrix_user_id,
        auth_user.user_id,
    }:
        raise HTTPException(status_code=403, detail="Private Dynamic Workflow report access denied.")
