"""Dynamic Workflow API routes."""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from mindroom.api.config_lifecycle import api_runtime_paths
from mindroom.constants import OWNER_MATRIX_USER_ID_ENV
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowRun, DynamicWorkflowStore

router = APIRouter(tags=["dynamic-workflows"])
public_router = APIRouter(tags=["dynamic-workflows"])

_REPORT_CSP = (
    "default-src 'none'; "
    "img-src 'self' data: https:; "
    "style-src 'unsafe-inline'; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


class _RuntimePathsProtocol(Protocol):
    def env_value(self, name: str, *, default: str | None = None) -> str | None: ...


@public_router.get("/reports/public/{slug}", include_in_schema=False)
async def public_dynamic_workflow_report(request: Request, slug: str) -> FileResponse:
    """Serve one active public Dynamic Workflow HTML report from runtime storage."""
    runtime_paths = api_runtime_paths(request)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    try:
        report_path = store.public_report_html_path(slug)
    except DynamicWorkflowError as exc:
        raise HTTPException(status_code=404, detail="Public Dynamic Workflow report was not found.") from exc

    response = FileResponse(report_path, media_type="text/html")
    _set_report_headers(response, cache_control="no-store, max-age=0")
    return response


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
    _set_report_headers(response, cache_control="private, no-store, max-age=0")
    return response


def _set_report_headers(response: FileResponse, *, cache_control: str) -> None:
    response.headers["Content-Security-Policy"] = _REPORT_CSP
    response.headers["Cache-Control"] = cache_control
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _authorize_private_report_request(
    request: Request,
    run: DynamicWorkflowRun,
    runtime_paths: _RuntimePathsProtocol,
) -> None:
    auth_user = request.scope.get("auth_user")
    if not isinstance(auth_user, dict):
        raise HTTPException(status_code=403, detail="Private Dynamic Workflow report access denied.")
    if run.requested_by in _private_report_auth_principals(auth_user, runtime_paths):
        return
    raise HTTPException(status_code=403, detail="Private Dynamic Workflow report access denied.")


def _private_report_auth_principals(
    auth_user: dict[str, object],
    runtime_paths: _RuntimePathsProtocol,
) -> set[str]:
    principals: set[str] = set()
    matrix_user_id = auth_user.get("matrix_user_id")
    if isinstance(matrix_user_id, str) and matrix_user_id:
        principals.add(matrix_user_id)

    user_id = auth_user.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return principals
    if user_id != "standalone":
        principals.add(user_id)
        return principals
    if auth_user.get("auth_source") == "trusted_upstream":
        return principals

    principals.add(user_id)
    owner_user_id = runtime_paths.env_value(OWNER_MATRIX_USER_ID_ENV)
    if owner_user_id:
        principals.add(owner_user_id)
    return principals
