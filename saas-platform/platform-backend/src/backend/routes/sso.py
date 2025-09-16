"""SSO cookie management routes."""

from datetime import timedelta
from typing import Annotated

from backend.config import PLATFORM_DOMAIN
from backend.deps import _extract_bearer_token, limiter, verify_user
from backend.models import StatusResponse
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

router = APIRouter()


@router.post("/my/sso-cookie", response_model=StatusResponse)
@limiter.limit("5/minute")
async def set_sso_cookie(
    request: Request,
    response: Response,
    user: dict = Depends(verify_user),  # noqa: ARG001, FAST002, B008
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Set a superdomain SSO cookie with the current Supabase access token.

    The cookie is used by instance nginx to forward Authorization headers.
    """
    try:
        token = _extract_bearer_token(authorization or request.headers.get("authorization"))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Missing bearer token") from None

    # Normalize cookie domain: ensure it applies to all subdomains
    domain = PLATFORM_DOMAIN
    if domain and not domain.startswith("."):
        domain = f".{domain}"

    # Set HttpOnly cookie valid for all subdomains (platform + instances)
    response.set_cookie(
        key="mindroom_jwt",
        value=token,
        domain=domain,  # e.g., .staging.mindroom.chat
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
    # Normalize cookie domain: ensure it applies to all subdomains
    domain = PLATFORM_DOMAIN
    if domain and not domain.startswith("."):
        domain = f".{domain}"

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
    return {"status": "cleared"}
