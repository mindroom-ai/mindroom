"""Budget status and per-user caps for the dashboard and the dedicated budget service."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_budget_service, verify_user
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.matrix_identifiers import is_concrete_matrix_user_id
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

__all__ = [
    "clear_service_user_cap",
    "clear_user_cap",
    "get_budgets",
    "get_service_budgets",
    "router",
    "set_service_user_cap",
    "set_user_cap",
]

router = APIRouter(prefix="/api/budgets", tags=["budgets"])

_NO_STORE = {"Cache-Control": "no-store"}
_DASHBOARD_UPDATED_BY = "dashboard"
_SERVICE_UPDATED_BY = "budget-service"


class _CapRequest(BaseModel):
    """One user's monthly cap in USD."""

    model_config = ConfigDict(extra="forbid")

    limit_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)


def _cap_amount(body: dict[str, Any]) -> float:
    """Validate a cap body without echoing its input, which can be a non-JSON value such as NaN."""
    try:
        return _CapRequest.model_validate(body).limit_usd
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.errors(include_input=False, include_context=False, include_url=False),
            headers=_NO_STORE,
        ) from exc


@router.get("", dependencies=[Depends(verify_user)], response_model=None)
def get_budgets(request: Request) -> JSONResponse:
    """Return budget settings and each user's month-to-date spend to the dashboard."""
    return JSONResponse(_monitor(request).status().to_dict(), headers=_NO_STORE)


@router.get("/service", dependencies=[Depends(require_budget_service)], response_model=None)
def get_service_budgets(request: Request) -> JSONResponse:
    """Return the same budget status to the authenticated budget service."""
    return get_budgets(request)


@router.put("/users/{user_id}", dependencies=[Depends(verify_user)], response_model=None)
def set_user_cap(request: Request, user_id: str, body: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """Create or replace one user's cap from the dashboard."""
    return _set_cap(request, user_id, _cap_amount(body), updated_by=_DASHBOARD_UPDATED_BY)


@router.put("/service/users/{user_id}", dependencies=[Depends(require_budget_service)], response_model=None)
def set_service_user_cap(request: Request, user_id: str, body: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """Create or replace one user's cap for the budget service."""
    return _set_cap(request, user_id, _cap_amount(body), updated_by=_SERVICE_UPDATED_BY)


@router.delete("/users/{user_id}", dependencies=[Depends(verify_user)], response_model=None)
def clear_user_cap(request: Request, user_id: str) -> Response:
    """Remove one user's cap from the dashboard, so the default cap applies."""
    return _clear_cap(request, user_id)


@router.delete("/service/users/{user_id}", dependencies=[Depends(require_budget_service)], response_model=None)
def clear_service_user_cap(request: Request, user_id: str) -> Response:
    """Remove one user's cap for the budget service, so the default cap applies."""
    return _clear_cap(request, user_id)


def _monitor(request: Request) -> BudgetMonitor:
    monitor = config_lifecycle.app_state(request.app).budget_monitor
    if monitor is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Budget monitor unavailable", headers=_NO_STORE)
    return monitor


def _cap_target(request: Request, user_id: str) -> tuple[BudgetMonitor, str]:
    """Return the runtime monitor and the canonical person a cap for ``user_id`` belongs to."""
    monitor = _monitor(request)
    config = monitor.config_provider()
    if config is None or config.budgets is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Budgets are off", headers=_NO_STORE)
    if not is_concrete_matrix_user_id(user_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "User ID must be a concrete Matrix user ID",
            headers=_NO_STORE,
        )
    # A bridge alias shares its canonical user's spend, so it shares one cap too.
    canonical_user_id = resolve_human_requester_alias(user_id, config, monitor.runtime_paths)
    if not is_human_requester_id(canonical_user_id, config, monitor.runtime_paths):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Caps apply only to people, not agents or bots",
            headers=_NO_STORE,
        )
    return monitor, canonical_user_id


def _set_cap(request: Request, user_id: str, limit_usd: float, *, updated_by: str) -> JSONResponse:
    monitor, canonical_user_id = _cap_target(request, user_id)
    cap = monitor.caps.set(canonical_user_id, limit_usd, updated_by=updated_by, now=datetime.now(UTC))
    return JSONResponse(
        {
            "user_id": canonical_user_id,
            "limit_usd": cap.limit_usd,
            "updated_at": cap.updated_at.isoformat(),
            "updated_by": cap.updated_by,
        },
        headers=_NO_STORE,
    )


def _clear_cap(request: Request, user_id: str) -> Response:
    monitor, canonical_user_id = _cap_target(request, user_id)
    monitor.caps.delete(canonical_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)
