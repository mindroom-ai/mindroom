"""Dashboard API for content-free retained token usage."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_personal_user, require_usage_service, verify_user
from mindroom.api.usage_export import (
    RETRY_AFTER_SECONDS,
    UsageExportContext,
    UsageExportStatus,
    context_is_current,
    usage_export_runner,
)
from mindroom.usage_stats import UsageReport, collect_admin_usage, collect_private_usage

__all__ = ["get_private_usage", "get_usage", "get_usage_export", "router"]

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _report_payload(report: UsageReport) -> dict[str, object]:
    """Stamp completed scans once, so cached exports retain their generation time."""
    return {**report.to_dict(), "schema_version": 1, "generated_at": datetime.now(UTC).isoformat()}


@router.get("", dependencies=[Depends(verify_user)], response_model=None)
def get_usage(
    request: Request,
    include_daily: bool = False,
    include_requests: bool = False,
) -> dict[str, object] | Response:
    """Prepare retained usage under standard dashboard authentication."""
    return _get_organization_usage(request, include_daily=include_daily, include_requests=include_requests)


@router.get("/export", dependencies=[Depends(require_usage_service)], response_model=None)
def get_usage_export(
    request: Request,
    include_daily: bool = False,
    include_requests: bool = False,
) -> dict[str, object] | Response:
    """Return retained usage to the authenticated usage-export service."""
    return _get_organization_usage(request, include_daily=include_daily, include_requests=include_requests)


def _get_organization_usage(
    request: Request,
    *,
    include_daily: bool,
    include_requests: bool,
) -> dict[str, object] | Response:
    """Start or poll one application-scoped organization usage report."""
    headers = {"Cache-Control": "no-store"}
    unavailable = Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers=headers)
    snapshot = config_lifecycle.request_snapshot(request)
    if snapshot is None:
        return unavailable
    try:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    except HTTPException:
        return unavailable
    api_app = request.app
    context = UsageExportContext(
        runtime_paths=runtime_paths,
        generation=snapshot.generation,
        include_daily=include_daily,
        include_requests=include_requests,
    )
    poll = usage_export_runner(api_app).poll(
        context,
        lambda: _report_payload(
            collect_admin_usage(
                config=config,
                runtime_paths=runtime_paths,
                include_daily=include_daily,
                include_requests=include_requests,
            ),
        ),
        context_is_current=lambda: context_is_current(api_app, context),
    )
    if poll.status is UsageExportStatus.PENDING:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "pending"}, headers=headers)
    if poll.status is UsageExportStatus.FAILED or poll.report is None:
        return unavailable
    return JSONResponse(content=poll.report, headers=headers)


@router.get("/me/private-agents")
def get_private_usage(
    request: Request,
    response: Response,
    user: Annotated[dict[str, Any], Depends(require_personal_user)],
    include_daily: bool = False,
) -> dict[str, object]:
    """Return only the signed requester's private-agent usage, without target overrides."""
    if set(request.query_params) - {"include_daily"}:
        raise HTTPException(status_code=400, detail="Usage target overrides are not accepted")
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    report = collect_private_usage(
        requester_id=user["matrix_user_id"],
        config=config,
        runtime_paths=runtime_paths,
        include_daily=include_daily,
    )
    response.headers["Cache-Control"] = "no-store"
    return _report_payload(report)
