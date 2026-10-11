"""Budget status and per-user caps for the dashboard and the dedicated budget service."""

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_budget_service, verify_user

__all__ = ["get_budgets", "get_service_budgets", "router"]

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


@router.get("", dependencies=[Depends(verify_user)], response_model=None)
def get_budgets(request: Request) -> JSONResponse:
    """Return budget settings and each user's month-to-date spend to the dashboard."""
    return _budget_status(request)


@router.get("/service", dependencies=[Depends(require_budget_service)], response_model=None)
def get_service_budgets(request: Request) -> JSONResponse:
    """Return the same budget status to the authenticated budget service."""
    return _budget_status(request)


def _budget_status(request: Request) -> JSONResponse:
    """Return budget settings and each user's month-to-date spend from the runtime's latest scan."""
    headers = {"Cache-Control": "no-store"}
    monitor = config_lifecycle.app_state(request.app).budget_monitor
    if monitor is None:
        return JSONResponse(
            {"detail": "Budget monitor unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers=headers,
        )
    return JSONResponse(monitor.status().to_dict(), headers=headers)
