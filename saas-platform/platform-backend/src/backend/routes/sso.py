"""SSO cookie management routes."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from backend.config import PLATFORM_DOMAIN
from backend.deps import _extract_bearer_token, limiter, verify_user
from backend.models import StatusResponse

router = APIRouter()


def _legacy_superdomain_cookie_domain() -> str | None:
    """Return old wildcard cookie domain so responses can revoke it."""
    domain = PLATFORM_DOMAIN.strip()
    if not domain:
        return None
    return domain if domain.startswith(".") else f".{domain}"


def _clear_legacy_superdomain_cookie(response: Response) -> None:
    """Clear legacy tenant-wide JWT cookie without setting new raw token there."""
    domain = _legacy_superdomain_cookie_domain()
    if not domain:
        return
    response.set_cookie(
        key="mindroom_jwt",
        value="",
        domain=domain,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
        max_age=0,
    )


@router.post("/my/sso-cookie", response_model=StatusResponse)
@limiter.limit("30/minute")
async def set_sso_cookie(
    request: Request,
    response: Response,
    user: dict = Depends(verify_user),  # noqa: ARG001, FAST002, B008
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Set an API-host-only SSO cookie with the current Supabase access token.

    Matrix OIDC uses this cookie on the platform API host.
    """
    try:
        token = _extract_bearer_token(authorization or request.headers.get("authorization"))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Missing bearer token") from None

    _clear_legacy_superdomain_cookie(response)

    # Host-only cookie: do not send raw platform JWT to tenant subdomains.
    response.set_cookie(
        key="mindroom_jwt",
        value=token,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
        max_age=int(timedelta(hours=1).total_seconds()),
    )
    return {"status": "ok"}


@router.delete("/my/sso-cookie", response_model=StatusResponse)
@limiter.limit("10/minute")
async def clear_sso_cookie(request: Request, response: Response) -> dict[str, str]:  # noqa: ARG001
    """Clear the SSO cookie on logout."""
    _clear_legacy_superdomain_cookie(response)

    response.set_cookie(key="mindroom_jwt", value="", path="/", secure=True, httponly=True, samesite="lax", max_age=0)
    return {"status": "cleared"}
