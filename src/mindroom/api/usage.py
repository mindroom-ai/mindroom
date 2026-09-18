"""Dashboard API for aggregate-only retained token usage."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_personal_user, require_usage_service, verify_user
from mindroom.usage_stats import collect_admin_usage, collect_private_usage

__all__ = ["get_private_usage", "get_usage", "get_usage_export", "router"]

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("", dependencies=[Depends(verify_user)])
def get_usage(request: Request, response: Response, include_daily: bool = False) -> dict[str, object]:
    """Return retained usage by user and model under dashboard administrator auth."""
    return _get_admin_usage(request, response, include_daily=include_daily)


@router.get("/export", dependencies=[Depends(require_usage_service)])
def get_usage_export(request: Request, response: Response, include_daily: bool = False) -> dict[str, object]:
    """Return retained usage to the authenticated usage-export service."""
    return _get_admin_usage(request, response, include_daily=include_daily)


def _get_admin_usage(request: Request, response: Response, *, include_daily: bool) -> dict[str, object]:
    """Build one aggregate administrator usage report for an authenticated caller."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    # FastAPI runs synchronous handlers in its thread pool, keeping SQLite scans
    # and report serialization off the runtime event loop.
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths, include_daily=include_daily)
    response.headers["Cache-Control"] = "no-store"
    return report.to_dict()


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
    return report.to_dict()
