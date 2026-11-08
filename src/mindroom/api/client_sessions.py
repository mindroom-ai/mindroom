"""Matrix web clients trade an OpenID token for a short-lived in-memory session that plugin routes accept."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.matrix_openid import (
    MatrixOpenIDError,
    MatrixOpenIDToken,
    homeserver_openid_binding,
    verify_matrix_openid,
)
from mindroom.origins import allowed_client_origins
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

_CLIENT_SESSION_TTL_SECONDS = 3600
# About 13 MB when full; high enough that sign-ins evict only their own user's sessions in practice.
_CLIENT_SESSION_CAPACITY = 10_000
_CLIENT_SESSIONS_PER_USER = 8
_BEARER_PREFIX = "bearer "
# A browser cannot set a websocket's headers, so a websocket offers this subprotocol, which the server accepts with,
# beside this one followed by `.<session_token>`, which the server never echoes.
SESSION_PROTOCOL = "mindroom-session"
_SESSION_TOKEN_PREFIX = f"{SESSION_PROTOCOL}."

router = APIRouter(prefix="/api/client-sessions")


@dataclass(frozen=True)
class ClientSession:
    """One signed-in Matrix user's bearer token, its Unix-seconds expiry, and the event its revocation sets."""

    token: str = field(repr=False)
    user_id: str
    expires_at: float
    revoked: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)


class ClientSessionStore:
    """Hold client sessions in memory only; a restart signs everyone out.

    Memory is bounded without refusing anyone: past the per-user cap that user's oldest session goes, and past the
    global cap the oldest of anyone's, so below the global cap one user's sign-ins never sign another user out.
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
        self._sessions: dict[str, ClientSession] = {}
        self._tokens_by_user: dict[str, list[str]] = {}

    def revoke(self, token: str) -> None:
        """End ``token``'s session at once; the token must name a stored session."""
        session = self._sessions.pop(token)
        tokens = self._tokens_by_user[session.user_id]
        tokens.remove(token)
        if not tokens:
            del self._tokens_by_user[session.user_id]
        session.revoked.set()

    def create(self, user_id: str) -> ClientSession:
        """Start a session for ``user_id`` that lasts :data:`_CLIENT_SESSION_TTL_SECONDS`."""
        now = self.clock()
        while self._sessions and (oldest := next(iter(self._sessions.values()))).expires_at <= now:
            self.revoke(oldest.token)
        while len(self._tokens_by_user.get(user_id, ())) >= self.per_user_capacity:
            self.revoke(self._tokens_by_user[user_id][0])
        while len(self._sessions) >= self.capacity:
            self.revoke(next(iter(self._sessions)))
        session = ClientSession(secrets.token_urlsafe(32), user_id, now + _CLIENT_SESSION_TTL_SECONDS)
        self._sessions[session.token] = session
        self._tokens_by_user.setdefault(user_id, []).append(session.token)
        return session

    def lookup(self, token: str) -> ClientSession | None:
        """Return the live session for ``token``, or ``None`` when it is unknown or expired."""
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= self.clock():
            self.revoke(token)
            return None
        return session


class _ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openid_token: MatrixOpenIDToken


def client_session_store(app: FastAPI) -> ClientSessionStore:
    """Return the app's session store, creating it on first use."""
    state = ensure_app_state(app)
    if state.client_sessions is None:
        state.client_sessions = ClientSessionStore()
    return state.client_sessions


def presented_session(request: Request) -> ClientSession:
    """Return the live session ``Authorization: Bearer <session_token>`` presents, or answer 401."""
    header = request.headers.get("Authorization", "")
    return _live_session(
        request.app,
        header[len(_BEARER_PREFIX) :].strip() if header.lower().startswith(_BEARER_PREFIX) else "",
    )


def offered_subprotocols(websocket: WebSocket) -> list[str]:
    """Return the subprotocols a websocket's client offers."""
    # Uvicorn SansIO can expose comma-separated header values instead of tokens.
    return [offer.strip() for value in websocket.scope.get("subprotocols", []) for offer in value.split(",")]


def offered_session(websocket: WebSocket) -> ClientSession:
    """Return the live session a websocket offers as ``mindroom-session.<session_token>``, or answer 401.

    The client offers ``mindroom-session`` beside it, which the server accepts with, and no other token.
    """
    offered = offered_subprotocols(websocket)
    tokens = [offer.removeprefix(_SESSION_TOKEN_PREFIX) for offer in offered if offer.startswith(_SESSION_TOKEN_PREFIX)]
    return _live_session(websocket.app, tokens[0] if SESSION_PROTOCOL in offered and len(tokens) == 1 else "")


def _live_session(app: FastAPI, token: str) -> ClientSession:
    store = ensure_app_state(app).client_sessions
    session = store.lookup(token) if store is not None else None
    if session is None:
        raise HTTPException(401, "Missing, invalid or expired client session.", headers={"WWW-Authenticate": "Bearer"})
    return session


def allowed_client_origin(websocket: WebSocket) -> bool:
    """Return whether a websocket comes from an origin ``MINDROOM_COMPUTER_ALLOWED_ORIGINS`` lists.

    CORS lets the same origins call the client API routes, but browsers do not apply it to websockets.
    """
    paths = config_lifecycle.require_api_state(websocket.app).snapshot.runtime_paths
    return websocket.headers.get("origin") in allowed_client_origins(paths, COMPUTER_ALLOWED_ORIGINS_ENV)


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
    session = client_session_store(request.app).create(user_id)
    return {"session_token": session.token, "user_id": session.user_id, "expires_at": int(session.expires_at)}


@router.delete("", status_code=204)
async def revoke_client_session(request: Request) -> None:
    """End the presented session at once, as a client does at sign-out; its token then answers 401 everywhere."""
    session = presented_session(request)
    client_session_store(request.app).revoke(session.token)
