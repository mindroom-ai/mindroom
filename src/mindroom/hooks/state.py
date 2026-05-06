"""Hook-to-Matrix room state query and write helpers."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast

import nio

if TYPE_CHECKING:
    from .types import HookRoomStatePutter, HookRoomStateQuerier


async def _query_hook_room_state(
    client: nio.AsyncClient,
    room_id: str,
    event_type: str,
    state_key: str | None = None,
) -> dict[str, Any] | None:
    """Query Matrix room state with hook adapter semantics."""
    if state_key is not None:
        resp = await client.room_get_state_event(room_id, event_type, state_key)
        if isinstance(resp, nio.RoomGetStateEventError):
            return None
        return resp.content

    resp = await client.room_get_state(room_id)
    if isinstance(resp, nio.RoomGetStateError):
        return None
    return {ev["state_key"]: ev["content"] for ev in resp.events if ev.get("type") == event_type}


def build_hook_room_state_querier(
    client: nio.AsyncClient,
) -> HookRoomStateQuerier:
    """Return a querier bound to one Matrix client.

    The returned adapter queries room state events via the Matrix client.
    When *state_key* is provided it fetches a single state event; when
    ``None`` it returns all events matching *event_type* as a
    ``{state_key: content}`` dict. Matrix error response objects are
    converted to ``None``; transport exceptions from the client
    propagate to the caller.
    """
    return cast("HookRoomStateQuerier", partial(_query_hook_room_state, client))


async def _put_hook_room_state(
    client: nio.AsyncClient,
    room_id: str,
    event_type: str,
    state_key: str,
    content: dict[str, Any],
) -> bool:
    """Write Matrix room state with hook adapter semantics."""
    resp = await client.room_put_state(room_id, event_type, content, state_key=state_key)
    return not isinstance(resp, nio.RoomPutStateError)


def build_hook_room_state_putter(
    client: nio.AsyncClient,
) -> HookRoomStatePutter:
    """Return a putter bound to one Matrix client.

    The returned adapter writes a single room state event via the Matrix
    client and returns ``True`` on success, ``False`` on Matrix error
    response. Transport exceptions from the client propagate to the
    caller.
    """
    return cast("HookRoomStatePutter", partial(_put_hook_room_state, client))


def chain_hook_room_state_queriers(
    primary: HookRoomStateQuerier | None,
    fallback: HookRoomStateQuerier | None,
) -> HookRoomStateQuerier | None:
    """Return a room-state querier that falls back on Matrix error responses."""
    if primary is None:
        return fallback
    if fallback is None:
        return primary

    async def _query(room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        result = await primary(room_id, event_type, state_key)
        if result is not None:
            return result
        return await fallback(room_id, event_type, state_key)

    return _query


def chain_hook_room_state_putters(
    primary: HookRoomStatePutter | None,
    fallback: HookRoomStatePutter | None,
) -> HookRoomStatePutter | None:
    """Return a room-state putter that falls back on Matrix error responses."""
    if primary is None:
        return fallback
    if fallback is None:
        return primary

    async def _put(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        if await primary(room_id, event_type, state_key, content):
            return True
        return await fallback(room_id, event_type, state_key, content)

    return _put
