"""SSO cookie management routes."""

from datetime import timedelta
from typing import Annotated

from backend.deps import _extract_bearer_token, limiter, verify_user
from backend.models import StatusResponse
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

router = APIRouter()


def _expire_sso_cookie(response: Response) -> None:
    response.set_cookie(
        key="mindroom_jwt",
        value="",
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
    """Set an API-host SSO cookie with the current Supabase access token.

    Tenant subdomains must not receive raw platform JWTs.
    """
    try:
        token = _extract_bearer_token(authorization or request.headers.get("authorization"))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Missing bearer token") from None

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
    _expire_sso_cookie(response)
    return {"status": "cleared"}
