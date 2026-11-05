"""Matrix web clients trade an OpenID token for a short-lived in-memory session that plugin routes accept."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    homeserver_openid_binding,
    verify_matrix_openid,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

_CLIENT_SESSION_TTL_SECONDS = 3600
_CLIENT_SESSION_CAPACITY = 256
_CLIENT_SESSIONS_PER_USER = 8

router = APIRouter(prefix="/api/client-sessions")


@dataclass(frozen=True)
class _ClientSession:
    """One signed-in Matrix user's bearer token and its Unix-seconds expiry."""

    token: str = field(repr=False)
    user_id: str
    expires_at: float


class ClientSessionStore:
    """Hold client sessions in memory only; a restart signs everyone out.

    Memory is bounded without refusing anyone: past a per-user or global cap the oldest session goes.
    The TTL is constant, so insertion order is expiry order and pruning stops at the first live session.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        capacity: int = _CLIENT_SESSION_CAPACITY,
        per_user_capacity: int = _CLIENT_SESSIONS_PER_USER,
    ) -> None:
        self.clock = clock
        self.capacity = capacity
        self.per_user_capacity = per_user_capacity
        self._sessions: dict[str, _ClientSession] = {}
        self._tokens_by_user: dict[str, list[str]] = {}

    def _drop(self, token: str) -> None:
        session = self._sessions.pop(token)
        tokens = self._tokens_by_user[session.user_id]
        tokens.remove(token)
        if not tokens:
            del self._tokens_by_user[session.user_id]

    def create(self, user_id: str) -> _ClientSession:
        """Start a session for ``user_id`` that lasts :data:`_CLIENT_SESSION_TTL_SECONDS`."""
        now = self.clock()
        while self._sessions and (oldest := next(iter(self._sessions.values()))).expires_at <= now:
            self._drop(oldest.token)
        while len(self._tokens_by_user.get(user_id, ())) >= self.per_user_capacity:
            self._drop(self._tokens_by_user[user_id][0])
        while len(self._sessions) >= self.capacity:
            self._drop(next(iter(self._sessions)))
        session = _ClientSession(secrets.token_urlsafe(32), user_id, now + _CLIENT_SESSION_TTL_SECONDS)
        self._sessions[session.token] = session
        self._tokens_by_user.setdefault(user_id, []).append(session.token)
        return session

    def lookup(self, token: str) -> _ClientSession | None:
        """Return the live session for ``token``, or ``None`` when it is unknown or expired."""
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= self.clock():
            self._drop(token)
            return None
        return session


class _ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openid_token: MatrixOpenIDToken


def _client_session_store(app: FastAPI) -> ClientSessionStore:
    """Return the app's session store, creating it on first use."""
    state = ensure_app_state(app)
    if state.client_sessions is None:
        state.client_sessions = ClientSessionStore()
    return state.client_sessions


async def _verified_user(token: MatrixOpenIDToken, paths: RuntimePaths, request: Request) -> str:
    try:
        return await verify_matrix_openid(token, paths, request_origin=str(request.base_url))
    except MatrixOpenIDError as error:
        raise HTTPException(error.status_code, error.detail) from None


@router.post("")
async def exchange_openid_token(payload: _ExchangeRequest, request: Request) -> dict[str, str | int]:
    """Exchange a configured-homeserver OpenID token for a client session token.

    Without binding, any backend a user opens from the client could replay their token here. `/versions` shows only
    that binding was on after the first verification, so the token is verified again afterwards, as Connections'
    dashboard sign-in does: a binding switch between the requests cannot then pass an unbound token in either direction.
    """
    paths = config_lifecycle.require_api_state(request.app).snapshot.runtime_paths
    user_id = await _verified_user(payload.openid_token, paths, request)
    binding = await homeserver_openid_binding(paths)
    if binding is None:
        raise HTTPException(503, "Could not check whether the homeserver binds OpenID tokens.")
    if not binding:
        raise HTTPException(403, "Client sessions need a homeserver that binds OpenID tokens to this deployment.")
    if await _verified_user(payload.openid_token, paths, request) != user_id:
        raise HTTPException(401, "Matrix OpenID verification failed.")
    session = _client_session_store(request.app).create(user_id)
    return {"session_token": session.token, "user_id": session.user_id, "expires_at": int(session.expires_at)}
