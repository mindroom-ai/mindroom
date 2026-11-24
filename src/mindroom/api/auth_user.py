"""The authenticated identity of one API request."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from fastapi import HTTPException

from mindroom.constants import OWNER_MATRIX_USER_ID_ENV
from mindroom.matrix.identity import try_parse_historical_matrix_user_id

if TYPE_CHECKING:
    from starlette.requests import Request

    from mindroom.constants import RuntimePaths

_AUTH_USER_SCOPE_KEY = "auth_user"
# A standalone login (API key or open access) names no one; its `user_id` is this sentinel.
STANDALONE_USER_ID = "standalone"


class AuthSource(StrEnum):
    """How a request authenticated."""

    # The standalone API key, or open access from an allowed origin.
    STANDALONE = "standalone"
    # A hosted platform session or Supabase bearer token.
    PLATFORM = "platform"
    # An identity a trusted gateway signed.
    TRUSTED_UPSTREAM = "trusted_upstream"
    # A Connections session from a Matrix OpenID sign-in.
    MATRIX_OPENID = "matrix_openid"


@dataclass(frozen=True)
class AuthUser:
    """One authenticated login."""

    user_id: str
    email: str | None
    source: AuthSource
    matrix_user_id: str | None = None

    def requester_id(self, runtime_paths: RuntimePaths) -> str | None:
        """Return the Matrix user this login acts as.

        A signed-in Matrix user acts as themselves, never as the owner; an operator login (standalone or hosted
        platform) acts as the configured owner, else as its own login id.
        """
        if self.source in {AuthSource.TRUSTED_UPSTREAM, AuthSource.MATRIX_OPENID}:
            return try_parse_historical_matrix_user_id(self.matrix_user_id)
        return runtime_paths.env_value(OWNER_MATRIX_USER_ID_ENV) or self.user_id


@dataclass(frozen=True)
class SignedMatrixUser(AuthUser):
    """A login that names its Matrix user: a Connections session, or a gateway identity with a Matrix ID."""

    matrix_user_id: str


def bind_request_auth_user[T: AuthUser](request: Request, auth_user: T) -> T:
    """Record the request's authenticated login."""
    request.scope[_AUTH_USER_SCOPE_KEY] = auth_user
    return auth_user


def request_auth_user(request: Request) -> AuthUser | None:
    """Return the request's authenticated login, None before authentication."""
    auth_user = request.scope.get(_AUTH_USER_SCOPE_KEY)
    return auth_user if isinstance(auth_user, AuthUser) else None


def require_request_auth_user(request: Request) -> AuthUser:
    """Return the request's authenticated login, or 401 when no login authenticated it."""
    auth_user = request_auth_user(request)
    if auth_user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")
    return auth_user
