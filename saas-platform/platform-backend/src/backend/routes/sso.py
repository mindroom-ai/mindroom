"""Platform SSO cookie and hosted-instance login routes."""

import re
import secrets
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import jwt
from backend.config import INSTANCE_BASE_DOMAIN, PLATFORM_DOMAIN
from backend.deps import _extract_bearer_token, ensure_supabase, limiter, verify_user
from backend.entitlements import assert_instance_entitlement
from backend.models import StatusResponse
from backend.services import instances_data, provisioner_service
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

router = APIRouter()

SSO_COOKIE_NAME = "mindroom_jwt"
SSO_COOKIE_MAX_AGE_SECONDS = int(timedelta(hours=1).total_seconds())
INSTANCE_SSO_TICKET_TYPE = "mindroom_platform_sso_ticket"
INSTANCE_SSO_TICKET_TTL_SECONDS = 60
_INSTANCE_ID_LABEL = re.compile(r"[1-9][0-9]*")


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


# LEGACY_COMPAT: Shared-domain SSO cookies sent to every tenant subdomain.
# Legacy format: `mindroom_jwt` cookies with `Domain=.PLATFORM_DOMAIN` when the platform domain is an ordinary DNS name.
# Last legacy release: v2026.9.278, which wrote them since v2026.6.147; replacement: the next release writes host-only cookies on the platform API host.
# Handling: Setting or clearing the cookie also expires the shared-domain cookie so browsers stop sending platform tokens to tenant hosts; localhost, IP, and single-label domains never had one.
# Coverage: saas-platform/platform-backend/tests/test_sso_cookie_attrs.py.
def _legacy_shared_sso_cookie_domain() -> str | None:
    domain = PLATFORM_DOMAIN.strip()
    host = domain.lstrip(".").lower()
    if not host or host == "localhost" or ":" in host or "." not in host:
        return None
    try:
        ip_address(host)
    except ValueError:
        return domain if domain.startswith(".") else f".{domain}"
    return None


def _expire_legacy_shared_sso_cookie(response: Response) -> None:
    domain = _legacy_shared_sso_cookie_domain()
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
    """Set a host-only platform API cookie with the current Supabase access token."""
    try:
        token = _extract_bearer_token(authorization or request.headers.get("authorization"))
    except HTTPException:
        raise HTTPException(status_code=401, detail="Missing bearer token") from None

    _expire_legacy_shared_sso_cookie(response)
    _set_cookie(response, value=token, max_age=SSO_COOKIE_MAX_AGE_SECONDS)
    return {"status": "ok"}


@router.delete("/my/sso-cookie", response_model=StatusResponse)
@limiter.limit("10/minute")
async def clear_sso_cookie(request: Request, response: Response) -> dict[str, str]:  # noqa: ARG001
    """Clear the SSO cookie on logout."""
    _set_cookie(response, value="", max_age=0)
    _expire_legacy_shared_sso_cookie(response)
    return {"status": "cleared"}


def _instance_dashboard_target(redirect_to: str) -> tuple[str, str, str]:
    """Return the instance id, origin, and in-app path of one hosted dashboard URL."""
    try:
        parsed = urlsplit(redirect_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid redirect_to") from None
    hostname = parsed.hostname or ""
    suffix = f".{(INSTANCE_BASE_DOMAIN or PLATFORM_DOMAIN).lower()}"
    instance_id = hostname.removesuffix(suffix)
    # Only canonical numeric labels: the integer column would otherwise match labels such as "01".
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != hostname
        or not hostname.endswith(suffix)
        or _INSTANCE_ID_LABEL.fullmatch(instance_id) is None
    ):
        raise HTTPException(status_code=400, detail="Invalid redirect_to")
    path = parsed.path or "/"
    return instance_id, f"https://{hostname}", f"{path}?{parsed.query}" if parsed.query else path


def platform_login_redirect(return_to: str) -> RedirectResponse:
    """Send the browser through platform login, then back to one platform API URL."""
    return RedirectResponse(f"https://app.{PLATFORM_DOMAIN}/auth/login?{urlencode({'redirect_to': return_to})}")


async def platform_cookie_user(request: Request) -> dict[str, Any] | None:
    """Return the user signed in by the API-host cookie, or None when platform login must run first."""
    token = request.cookies.get(SSO_COOKIE_NAME)
    if not token:
        return None
    try:
        return await verify_user(authorization=f"Bearer {token}", request=request)
    except HTTPException:
        return None


def assert_instance_login_allowed(instance_id: str, account_id: str) -> None:
    """Raise unless the account owns the instance and its subscription still allows signing in to it."""
    sb = ensure_supabase()
    instance = instances_data.get_owned_instance(sb, instance_id, account_id)
    if instance is None:
        raise HTTPException(status_code=403, detail="Instance not found or access denied")
    result = sb.table("subscriptions").select("*").eq("id", instance["subscription_id"]).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Subscription not found")
    assert_instance_entitlement(result.data[0], "sign in to")


# response_model=None: the slowapi wrapper keeps FastAPI from resolving the RedirectResponse annotation.
@router.get("/instance-sso/authorize", response_model=None)
@limiter.limit("60/minute")
async def authorize_instance_sso(request: Request, redirect_to: str) -> RedirectResponse:
    """Send one owned instance a short-lived login ticket signed with that instance's own key.

    The platform cookie never leaves the API host, and the ticket is not a platform credential.
    """
    instance_id, origin, next_path = _instance_dashboard_target(redirect_to)
    user = await platform_cookie_user(request)
    if user is None:
        return platform_login_redirect(
            f"https://api.{PLATFORM_DOMAIN}/instance-sso/authorize?{urlencode({'redirect_to': redirect_to})}"
        )
    assert_instance_login_allowed(instance_id, str(user["account_id"]))

    now = int(datetime.now(UTC).timestamp())
    ticket = jwt.encode(
        {
            "typ": INSTANCE_SSO_TICKET_TYPE,
            "aud": origin,
            "sub": str(user["user_id"]),
            "email": user.get("email"),
            "iat": now,
            "exp": now + INSTANCE_SSO_TICKET_TTL_SECONDS,
            "jti": secrets.token_urlsafe(24),
        },
        provisioner_service.instance_platform_sso_secret(instance_id),
        algorithm="HS256",
    )
    return RedirectResponse(f"{origin}/api/auth/platform-sso?{urlencode({'ticket': ticket, 'next': next_path})}")
