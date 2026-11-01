"""Connections portal sign-in: exchange a Matrix OpenID token for a portal session cookie."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, ValidationError

from mindroom.api import config_lifecycle
from mindroom.api.auth import is_administrator, require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    require_connections_public_origin,
    require_connections_same_origin,
    resolve_connection_user,
)
from mindroom.api.connections_session_cookie import (
    CONNECTIONS_SESSION_SECONDS,
    connections_session_cookie,
    issue_connections_session,
)
from mindroom.constants import RuntimePaths
from mindroom.logging_config import get_logger
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    homeserver_binds_openid_audience,
    verify_matrix_openid,
)
from mindroom.origins import connections_client_origin_allowed, connections_origins_configured, deployment_origin

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = get_logger(__name__)


class _SignInRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default validation response echoes the submitted input, including the token.
                return JSONResponse(
                    {"detail": "Invalid sign-in request"},
                    status_code=401,
                    headers=CONNECTIONS_HEADERS,
                )

        return handler


router = APIRouter(prefix="/api/connections/session", tags=["connections"], route_class=_SignInRoute)


class _SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openid_token: MatrixOpenIDToken
    client_origin: str


class _SignedIn(BaseModel):
    matrix_user_id: str


def _portal_paths(request: Request) -> RuntimePaths:
    """Gate the request before its body is validated, so a refused request never has its token validated or verified.

    FastAPI has already read and JSON-parsed the body by the time dependencies run.
    """
    paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    require_connections_same_origin(request, paths)
    return paths


_PortalPaths = Annotated[RuntimePaths, Depends(_portal_paths)]


async def _dashboard_allowed(
    openid_token: MatrixOpenIDToken,
    matrix_user_id: str,
    snapshot: config_lifecycle.ApiSnapshot,
) -> bool:
    """Return whether this sign-in may open the dashboard: an administrator whose token is bound to this deployment.

    The dashboard is full control, and an unbound token could have been replayed from another deployment on the same
    homeserver. `/versions` shows only that binding was on after the first verification, so the token is verified
    again afterwards: a binding switch between the requests cannot then pass an unbound token in either direction.
    """
    if not is_administrator(matrix_user_id, snapshot):
        return False
    if not await homeserver_binds_openid_audience(snapshot.runtime_paths):
        return False
    try:
        return await verify_matrix_openid(openid_token, snapshot.runtime_paths) == matrix_user_id
    except MatrixOpenIDError as error:
        logger.warning("Dashboard access check failed after the binding probe", detail=error.detail)
        return False


async def _start_session(
    openid_token: MatrixOpenIDToken,
    client_origin: str,
    request: Request,
    response: Response,
    paths: RuntimePaths,
) -> str:
    """Trust a Matrix OpenID token only from an allowlisted Chat origin, then set the portal session cookie."""
    # Without this allowlist a page the victim visits could hand the portal an attacker's token.
    origin = deployment_origin(paths.env_value("MINDROOM_PUBLIC_URL"), str(request.base_url))
    if not connections_client_origin_allowed(paths, client_origin, origin):
        raise HTTPException(403, "Connections sign-in is not allowed from this client", headers=CONNECTIONS_HEADERS)
    # Without binding, any backend a user opens from Chat could replay their token here. Request headers cannot show
    # that a portal is out of such a backend's reach, so only an operator's explicit list accepts that risk.
    if not connections_origins_configured(paths) and not await homeserver_binds_openid_audience(paths):
        raise HTTPException(
            403,
            "Connections sign-in from MindRoom Chat needs a homeserver that binds tokens, "
            "or MINDROOM_CONNECTIONS_ALLOWED_ORIGINS",
            headers=CONNECTIONS_HEADERS,
        )
    try:
        matrix_user_id = await verify_matrix_openid(openid_token, paths, request_origin=str(request.base_url))
    except MatrixOpenIDError as error:
        raise HTTPException(error.status_code, error.detail, headers=CONNECTIONS_HEADERS) from error
    # Any homeserver user can obtain a valid token, so only users with portal access get a session.
    app_state = config_lifecycle.app_state(request.app)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    user = resolve_connection_user(snapshot, matrix_user_id, membership_index=app_state.runtime.agent_reply_memberships)
    if not user.visible_agent_names:
        raise HTTPException(403, "No connections are available for this account", headers=CONNECTIONS_HEADERS)
    token = issue_connections_session(
        matrix_user_id,
        now=time.time(),
        dashboard=await _dashboard_allowed(openid_token, matrix_user_id, snapshot),
    )
    response.headers.update(CONNECTIONS_HEADERS)
    cookie = connections_session_cookie(origin)
    response.set_cookie(
        cookie,
        token,
        max_age=CONNECTIONS_SESSION_SECONDS,
        path="/",
        secure=cookie.startswith("__Host-"),
        httponly=True,
        samesite="lax",
    )
    return matrix_user_id


@router.post("")
async def sign_in(body: _SignIn, request: Request, response: Response, paths: _PortalPaths) -> _SignedIn:
    """Start a portal session from the token MindRoom Chat posted to the portal page."""
    # The portal page reports the postMessage origin it received the token from.
    matrix_user_id = await _start_session(body.openid_token, body.client_origin, request, response, paths)
    return _SignedIn(matrix_user_id=matrix_user_id)


@router.post("/handoff")
async def hand_off(request: Request, openid_token: Annotated[str, Form()]) -> RedirectResponse:
    """Start a portal session from the form MindRoom Chat's /connections page submits, then open the portal.

    The browser sets Origin on this cross-origin post, so the Chat allowlist decides which page may submit it.
    """
    paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    require_connections_public_origin(request, paths)
    try:
        token = MatrixOpenIDToken.model_validate_json(openid_token)
    except ValidationError:
        # The validation error would carry the submitted token.
        raise HTTPException(401, "Invalid sign-in request", headers=CONNECTIONS_HEADERS) from None
    redirect = RedirectResponse("/connections/", status_code=303)
    await _start_session(token, request.headers.get("origin", ""), request, redirect, paths)
    return redirect


@router.get("")
async def current_session(request: Request, response: Response) -> _SignedIn:
    """Tell the portal page which Matrix user is signed in, or 401 so it signs in."""
    try:
        auth_user = await require_connections_user(request)
    except HTTPException as error:
        # The sign-in probe answer depends on the browser's cookies, so no cache may keep it.
        raise HTTPException(
            error.status_code,
            error.detail,
            headers={**(error.headers or {}), **CONNECTIONS_HEADERS},
        ) from error
    response.headers.update(CONNECTIONS_HEADERS)
    return _SignedIn(matrix_user_id=auth_user.matrix_user_id)
