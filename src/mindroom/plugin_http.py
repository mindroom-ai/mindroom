"""Public helpers for plugin HTTP routes that serve a Matrix web client."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from mindroom.api.app_state import ensure_app_state

# Plugin routes import these from outside src/, so they stay public though no module here imports them.
__all__ = ["ClientUser", "require_client_user", "require_room_member"]

_BEARER_PREFIX = "bearer "


@dataclass(frozen=True)
class ClientUser:
    """The Matrix user a client session belongs to."""

    user_id: str


async def require_client_user(request: Request) -> ClientUser:
    """Resolve ``Authorization: Bearer <session_token>`` to its user, or answer 401."""
    header = request.headers.get("Authorization", "")
    store = ensure_app_state(request.app).client_sessions
    session = (
        store.lookup(header[len(_BEARER_PREFIX) :].strip())
        if store is not None and header.lower().startswith(_BEARER_PREFIX)
        else None
    )
    if session is None:
        raise HTTPException(401, "Missing, invalid or expired client session.", headers={"WWW-Authenticate": "Bearer"})
    return ClientUser(session.user_id)


async def require_room_member(request: Request, room_id: str, user_id: str) -> None:
    """Answer 503 when live membership is unavailable, or 403 unless ``user_id`` is joined to ``room_id``."""
    room_members = ensure_app_state(request.app).room_members
    if room_members is None:
        raise HTTPException(503, "Room membership is unavailable.")
    if user_id not in await room_members(room_id):
        raise HTTPException(403, "Not a member of this room.")
