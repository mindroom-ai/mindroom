"""Dashboard API for per-user monthly spending budgets."""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle

__all__ = ["get_budgets", "router"]

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


@router.get("", response_model=None)
def get_budgets(request: Request) -> JSONResponse:
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
