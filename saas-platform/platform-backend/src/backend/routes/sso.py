"""SSO cookie management routes."""

from datetime import timedelta
from ipaddress import ip_address
from typing import Annotated

from backend.config import PLATFORM_DOMAIN
from backend.deps import _extract_bearer_token, limiter, verify_user
from backend.models import StatusResponse
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

router = APIRouter()

SSO_COOKIE_NAME = "mindroom_jwt"
SSO_COOKIE_MAX_AGE_SECONDS = int(timedelta(hours=1).total_seconds())


def _sso_cookie_domain() -> str | None:
    domain = PLATFORM_DOMAIN.strip()
    host = domain.lstrip(".").lower()
    if not host or host == "localhost" or ":" in host or "." not in host:
        return None
    try:
        ip_address(host)
    except ValueError:
        return domain if domain.startswith(".") else f".{domain}"
    return None


def _set_cookie(
    response: Response,
    *,
    value: str,
    max_age: int,
    domain: str | None = None,
) -> None:
    kwargs = {
        "key": SSO_COOKIE_NAME,
        "value": value,
        "path": "/",
        "secure": True,
        "httponly": True,
        "samesite": "lax",
        "max_age": max_age,
    }
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def _expire_legacy_host_only_sso_cookie(response: Response) -> None:
    _set_cookie(response, value="", max_age=0)


def _expire_sso_cookie(response: Response) -> None:
    _expire_legacy_host_only_sso_cookie(response)
    domain = _sso_cookie_domain()
    if domain is not None:
        _set_cookie(response, value="", max_age=0, domain=domain)


@router.post(
    "/my/sso-cookie",
    response_model=StatusResponse,
    responses={401: {"description": "Missing bearer token"}},
)
@limiter.limit("30/minute")
async def set_sso_cookie(
    request: Request,
    response: Response,
    user: dict = Depends(verify_user),  # noqa: ARG001, FAST002, B008
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Set an SSO cookie with the current Supabase access token."""
    try:
        token = _extract_bearer_token(authorization or request.headers.get("authorization"))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Missing bearer token") from None

    _expire_legacy_host_only_sso_cookie(response)
    _set_cookie(response, value=token, max_age=SSO_COOKIE_MAX_AGE_SECONDS, domain=_sso_cookie_domain())
    return {"status": "ok"}


@router.delete("/my/sso-cookie", response_model=StatusResponse)
@limiter.limit("10/minute")
async def clear_sso_cookie(request: Request, response: Response) -> dict[str, str]:  # noqa: ARG001
    """Clear the SSO cookie on logout."""
    _expire_sso_cookie(response)
    return {"status": "cleared"}
