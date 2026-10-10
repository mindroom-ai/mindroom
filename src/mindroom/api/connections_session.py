"""Connections portal sign-in: exchange a Matrix OpenID token for a portal session cookie."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    connections_public_origin,
    require_connections_same_origin,
)
from mindroom.api.connections_sessions import CONNECTIONS_SESSION_COOKIE, CONNECTIONS_SESSION_SECONDS
from mindroom.constants import RuntimePaths
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    allowed_client_origins,
    verify_matrix_openid,
)
from mindroom.runtime_env_policy import CONNECTIONS_ALLOWED_ORIGINS_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


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


def _enabled_portal_paths(request: Request) -> RuntimePaths:
    paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    if not (paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip():
        raise HTTPException(404, "Connections are not enabled", headers=CONNECTIONS_HEADERS)
    return paths


def _portal_paths(request: Request) -> RuntimePaths:
    """Gate the request before its body is validated, so a refused request never has its token validated or verified.

    FastAPI has already read and JSON-parsed the body by the time dependencies run.
    """
    paths = _enabled_portal_paths(request)
    require_connections_same_origin(request, paths)
    return paths


_PortalPaths = Annotated[RuntimePaths, Depends(_portal_paths)]


@router.post("")
async def sign_in(body: _SignIn, request: Request, response: Response, paths: _PortalPaths) -> _SignedIn:
    """Trust a Matrix OpenID token only from an allowlisted Chat origin, then start a portal session."""
    # The portal page reports the postMessage origin it received the token from. Without this
    # allowlist a page the victim visits could hand the portal an attacker's token.
    if body.client_origin not in allowed_client_origins(paths, CONNECTIONS_ALLOWED_ORIGINS_ENV):
        raise HTTPException(403, "Connections sign-in is not allowed from this client", headers=CONNECTIONS_HEADERS)
    try:
        matrix_user_id = await verify_matrix_openid(
            body.openid_token,
            paths,
            audience=connections_public_origin(request, paths),
        )
    except MatrixOpenIDError as error:
        raise HTTPException(error.status_code, error.detail, headers=CONNECTIONS_HEADERS) from error
    token = config_lifecycle.app_state(request.app).connections_sessions.create(matrix_user_id)
    response.headers.update(CONNECTIONS_HEADERS)
    response.set_cookie(
        CONNECTIONS_SESSION_COOKIE,
        token,
        max_age=CONNECTIONS_SESSION_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return _SignedIn(matrix_user_id=matrix_user_id)


@router.get("")
async def current_session(request: Request, response: Response) -> _SignedIn:
    """Tell the portal page which Matrix user is signed in, or 401 so it signs in."""
    _enabled_portal_paths(request)
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
    return _SignedIn(matrix_user_id=auth_user["matrix_user_id"])
