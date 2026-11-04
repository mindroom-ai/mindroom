"""Connections portal sign-in from MindRoom Chat's iOS app, which confirms the browser session with its own token.

The app cannot post a token from a page in the system browser, and a code in a URL alone would sign in whichever
browser opened a forwarded link. So the browser that redeems the app's start state gets a pending cookie and the
app a nonce, and the session starts only in that browser, after the same user's app confirms the nonce.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle, connections_session
from mindroom.api.connection_agents import CONNECTIONS_HEADERS, require_connections_public_origin
from mindroom.api.connections_session_cookie import (
    CONNECTIONS_SESSION_SECONDS,
    connections_session_cookie,
    issue_connections_session,
)
from mindroom.matrix_openid import (
    MatrixOpenIDToken,  # noqa: TC001 - Pydantic resolves postponed annotations at runtime.
)
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state
from mindroom.origins import connections_sign_in_enabled

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

router = APIRouter(
    prefix="/api/connections/session/native",
    tags=["connections"],
    route_class=connections_session._SignInRoute,
)

_STATE_SECONDS = 120
_START = "connections_native_start"
_PENDING = "connections_native_pending"
_NONCE = "connections_native_nonce"
_CONFIRMED = "connections_native_confirmed"
_DEFAULT_NEXT = "/connections/"
# The portal, or an agent's connect or reset link, with an optional query of RFC 3986 query characters.
_NEXT_PATH = re.compile(
    r"(?:/connections/|/api/oauth/[A-Za-z0-9_-]+/(?:authorize|reset))(?:\?[A-Za-z0-9\-._~!$&'()*+,;=:@/?%]*)?",
)
_NEXT_FORBIDDEN = ("\\", "//", "%2f", "%5c", "..", "#")
_EXPIRED = "This sign-in expired or was already used"
_TAP_AGAIN_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sign-in expired</title>
  </head>
  <body>
    <h1>Sign-in expired</h1>
    <p>This sign-in expired or was already used. Tap the link in the MindRoom app again.</p>
  </body>
</html>
"""


# The sign-in sheet closes on the first navigation to the app's callback scheme. This page, not a redirect, performs it:
# iOS may drop a cookie set on a response that redirects straight to that scheme, and the flow needs the pending cookie.
_REDEEMED_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="0;url={url}">
    <title>Signing in</title>
  </head>
  <body>
    <h1>Signing in</h1>
    <p>Returning to the MindRoom app. If nothing happens, <a href="{url}">tap here to continue</a>.</p>
  </body>
</html>
"""


class _NativeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openid_token: MatrixOpenIDToken
    next: str | None = None


class _NativeStarted(BaseModel):
    redeem_url: str


class _NativeConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openid_token: MatrixOpenIDToken
    nonce: str


class _NativeConfirmed(BaseModel):
    continue_url: str


def _validated_native_next(value: str | None) -> str:
    """Return where the signed-in browser goes: the portal, or a same-origin OAuth connect or reset link."""
    if value is None:
        return _DEFAULT_NEXT
    if any(part in value.lower() for part in _NEXT_FORBIDDEN) or not _NEXT_PATH.fullmatch(value):
        raise HTTPException(400, "Invalid next path", headers=CONNECTIONS_HEADERS)
    return value


def _paths_and_origin(request: Request) -> tuple[RuntimePaths, str]:
    paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    return paths, require_connections_public_origin(request, paths)


async def _require_native_sign_in(paths: RuntimePaths) -> None:
    """Refuse when the operator turned sign-in off, or when the homeserver does not bind tokens.

    `MINDROOM_CONNECTIONS_ALLOWED_ORIGINS` is only the off switch here: the app has no web origin to list.
    No browser Origin vouches for these calls either, so the token is the only proof: an unbound one could be replayed
    here by any backend the user opened from Chat, which could then finish this flow in its own browser.
    """
    if not connections_sign_in_enabled(paths):
        raise HTTPException(403, connections_session.SIGN_IN_NOT_ALLOWED, headers=CONNECTIONS_HEADERS)
    if not await connections_session.homeserver_binds_openid_audience(paths):
        raise HTTPException(
            403,
            "Sign-in from the MindRoom app needs a homeserver that binds tokens",
            headers=CONNECTIONS_HEADERS,
        )


def _issue(paths: RuntimePaths, kind: str, **data: str) -> str:
    return issue_opaque_oauth_state(paths, kind=kind, ttl_seconds=_STATE_SECONDS, data=data)


def _digest(pending: str) -> str:
    return hashlib.sha256(pending.encode()).hexdigest()


def _pending_cookie(origin: str) -> str:
    return f"{connections_session_cookie(origin)}_native"


def _tap_again_page() -> HTMLResponse:
    return HTMLResponse(_TAP_AGAIN_PAGE, status_code=400, headers=CONNECTIONS_HEADERS)


def _redeemed_page(callback_url: str) -> HTMLResponse:
    return HTMLResponse(_REDEEMED_PAGE.format(url=html.escape(callback_url)), headers=CONNECTIONS_HEADERS)


@router.post("/start")
async def start_native_sign_in(body: _NativeStart, request: Request, response: Response) -> _NativeStarted:
    """Return the URL the app opens in the system browser to start signing it in as the token's user."""
    paths, origin = _paths_and_origin(request)
    next_path = _validated_native_next(body.next)
    await _require_native_sign_in(paths)
    user = await connections_session.verified_connections_user(body.openid_token, request, paths)
    start = _issue(paths, _START, user=user, next=next_path)
    response.headers.update(CONNECTIONS_HEADERS)
    return _NativeStarted(redeem_url=f"{origin}{router.prefix}/redeem?start={start}")


@router.get("/redeem")
async def redeem_native_start(request: Request, start: str | None = None) -> Response:
    """Mark this browser with a pending cookie and hand the app a nonce to confirm through its callback URL."""
    paths, origin = _paths_and_origin(request)
    if start is None:
        return _tap_again_page()
    try:
        started = consume_opaque_oauth_state(paths, kind=_START, token=start)
    except OAuthProviderError:
        return _tap_again_page()
    pending = _issue(paths, _PENDING, user=started["user"], next=started["next"])
    nonce = _issue(paths, _NONCE, user=started["user"], pending=_digest(pending))
    page = _redeemed_page(f"mindroom://connections/confirm?nonce={nonce}")
    connections_session.set_portal_cookie(page, _pending_cookie(origin), pending, max_age=_STATE_SECONDS)
    return page


@router.post("/confirm")
async def confirm_native_sign_in(body: _NativeConfirm, request: Request, response: Response) -> _NativeConfirmed:
    """Confirm the nonce with the app's own token, which must name the user who started this sign-in."""
    paths, origin = _paths_and_origin(request)
    await _require_native_sign_in(paths)
    # Consumed before the token is checked, so a refused confirmation cannot be retried.
    try:
        nonce = consume_opaque_oauth_state(paths, kind=_NONCE, token=body.nonce)
    except OAuthProviderError:
        raise HTTPException(400, _EXPIRED, headers=CONNECTIONS_HEADERS) from None
    user = await connections_session.verified_connections_user(body.openid_token, request, paths)
    if user != nonce["user"]:
        raise HTTPException(403, "This sign-in was started by another account", headers=CONNECTIONS_HEADERS)
    confirmed = _issue(paths, _CONFIRMED, user=user, pending=nonce["pending"])
    response.headers.update(CONNECTIONS_HEADERS)
    return _NativeConfirmed(continue_url=f"{origin}{router.prefix}/continue?confirmed={confirmed}")


@router.get("/continue")
async def continue_native_sign_in(request: Request, confirmed: str | None = None) -> Response:
    """Start the portal session in the browser holding the confirmed pending cookie, then open `next`."""
    paths, origin = _paths_and_origin(request)
    if confirmed is None:
        return _tap_again_page()
    try:
        confirmation = consume_opaque_oauth_state(paths, kind=_CONFIRMED, token=confirmed)
    except OAuthProviderError:
        return _tap_again_page()
    cookie = _pending_cookie(origin)
    pending = request.cookies.get(cookie)
    # Checked before the pending state is consumed, so a stale or forwarded URL leaves this browser's attempt intact.
    if pending is None or not hmac.compare_digest(_digest(pending), confirmation["pending"]):
        return _tap_again_page()
    try:
        signed_in = consume_opaque_oauth_state(paths, kind=_PENDING, token=pending)
    except OAuthProviderError:
        return _tap_again_page()
    if signed_in["user"] != confirmation["user"]:
        return _tap_again_page()
    redirect = RedirectResponse(signed_in["next"], status_code=303, headers=CONNECTIONS_HEADERS)
    # This flow serves the portal and agents' OAuth links, so its session never opens the dashboard.
    session = issue_connections_session(signed_in["user"], now=time.time(), dashboard=False)
    connections_session.set_portal_cookie(
        redirect,
        connections_session_cookie(origin),
        session,
        max_age=CONNECTIONS_SESSION_SECONDS,
    )
    # Max-Age=0 deletes the pending cookie.
    connections_session.set_portal_cookie(redirect, cookie, "", max_age=0)
    return redirect
