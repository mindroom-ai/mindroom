"""Public helpers for plugin HTTP routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import HTTPException, Path, Request, WebSocket, WebSocketDisconnect, status
from fastapi.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves dependency annotations.

from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import (
    SESSION_PROTOCOL,
    allowed_client_origin,
    client_session_store,
    offered_session,
    presented_session,
)
from mindroom.api.config_lifecycle import require_api_state
from mindroom.plugin_context import PluginContext, build_plugin_context
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.api.client_sessions import ClientSession
    from mindroom.config.main import Config

# Plugin routes import these from outside src/, so they stay public though no module here imports them.
__all__ = ["ClientUser", "client_stream", "plugin_context", "require_client_user", "require_room_member"]

# A stream in a room rechecks its user's membership this long after each check.
_MEMBERSHIP_RECHECK_SECONDS = 60.0
# The websocket counterpart of HTTP 401, from the private-use range: the client signs in again.
_SESSION_ENDED_CLOSE_CODE = 4401


@dataclass(frozen=True)
class ClientUser:
    """The Matrix user a client session belongs to."""

    user_id: str


async def require_client_user(request: Request) -> ClientUser:
    """Resolve ``Authorization: Bearer <session_token>`` to its user, or answer 401."""
    return ClientUser(presented_session(request).user_id)


async def require_room_member(request: Request, room_id: Annotated[str, Path()]) -> ClientUser:
    """Return the client session's user once they are joined to ``room_id``, answering 401, 403 or 503 otherwise.

    The user is always the session's: as a dependency, FastAPI fills every parameter from the request, so one naming
    the user would let the caller choose it. For the same reason ``room_id`` comes only from the route's path: a route
    without a ``{room_id}`` answers 422 rather than check a room the query string names.
    """
    user = await require_client_user(request)
    await _require_joined(request, user.user_id, room_id)
    return user


async def _require_joined(connection: HTTPConnection, user_id: str, room_id: str) -> None:
    room_members = ensure_app_state(connection.app).room_members
    if room_members is None:
        raise HTTPException(503, "Room membership is unavailable.")
    if user_id not in await room_members(room_id):
        raise HTTPException(403, "Not a member of this room.")


@asynccontextmanager
async def client_stream(websocket: WebSocket, room_id: str | None = None) -> AsyncIterator[ClientUser]:
    """Sign a websocket's caller in with their client session, accept it, and yield their user while it stays signed in.

    The client offers the subprotocols ``mindroom-session`` and ``mindroom-session.<session_token>``, from an origin
    ``MINDROOM_COMPUTER_ALLOWED_ORIGINS`` lists, and given ``room_id`` its user must be joined to that room. Before
    accepting, a refusal raises ``HTTPException``, which the handshake answers: 401 for no live session, 403 for
    another origin or a non-member, 503 when membership is unavailable. Once accepted with ``mindroom-session``, the
    block is cancelled where it waits and the websocket closed with 4401 when the session expires or is revoked, and
    given ``room_id``, with 1008 when a recheck finds the user gone or 1013 when one fails; code after the block runs.
    """
    if not allowed_client_origin(websocket):
        raise HTTPException(403, "Origin is not allowed.")
    session = offered_session(websocket)
    if room_id is not None:
        await _require_joined(websocket, session.user_id, room_id)
        # The session may have ended while membership was read.
        offered_session(websocket)
    await websocket.accept(subprotocol=SESSION_PROTOCOL)
    stream = asyncio.timeout(None)

    async def watch() -> tuple[int, str]:
        end = await _stream_end(websocket, session, room_id)
        # Cancels the block where it waits.
        stream.reschedule(asyncio.get_running_loop().time())
        return end

    watcher = asyncio.create_task(watch())
    try:
        async with stream:
            yield ClientUser(session.user_id)
    except TimeoutError:
        if not stream.expired():
            raise
        # The client may be gone already.
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(*watcher.result())
    finally:
        watcher.cancel()


async def _stream_end(websocket: WebSocket, session: ClientSession, room_id: str | None) -> tuple[int, str]:
    """Wait until the session ends or, given ``room_id``, a membership recheck fails; return the close code and reason."""
    ends = [asyncio.create_task(_session_end(websocket, session))]
    if room_id is not None:
        ends.append(asyncio.create_task(_membership_end(websocket, session.user_id, room_id)))
    try:
        # A revocation must not wait for a membership recheck that is still running.
        done, _ = await asyncio.wait(ends, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in ends:
            task.cancel()
    return done.pop().result()


async def _session_end(websocket: WebSocket, session: ClientSession) -> tuple[int, str]:
    store = client_session_store(websocket.app)
    while store.lookup(session.token) is not None:
        with suppress(TimeoutError):
            async with asyncio.timeout(session.expires_at - store.clock()):
                await session.revoked.wait()
    return _SESSION_ENDED_CLOSE_CODE, "Client session ended; sign in again."


async def _membership_end(websocket: WebSocket, user_id: str, room_id: str) -> tuple[int, str]:
    while True:
        await asyncio.sleep(_MEMBERSHIP_RECHECK_SECONDS)
        try:
            await _require_joined(websocket, user_id, room_id)
        except HTTPException as error:
            gone = error.status_code == status.HTTP_403_FORBIDDEN
            return status.WS_1008_POLICY_VIOLATION if gone else status.WS_1013_TRY_AGAIN_LATER, error.detail


def plugin_context(connection: HTTPConnection) -> PluginContext:
    """Return the context of the plugin whose route serves ``connection``, under the config the API serves now.

    Called again, as by a websocket after a config reload, it returns the newer config's context. It raises
    ``PluginInactiveError``, which the route answers with 503, when that config no longer loads the plugin.
    """
    plugin_name = connection.scope.get(PLUGIN_NAME_SCOPE_KEY)
    if plugin_name is None:
        msg = "plugin_context() serves only a plugin's own routes"
        raise RuntimeError(msg)
    state = require_api_state(connection.app)
    snapshot = state.snapshot

    def current_config() -> Config | None:
        latest = state.snapshot
        return latest.runtime_config if latest.runtime_paths == snapshot.runtime_paths else None

    return build_plugin_context(plugin_name, snapshot.runtime_config, snapshot.runtime_paths, current_config)
