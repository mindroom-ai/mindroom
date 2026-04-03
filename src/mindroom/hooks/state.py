"""Hook-to-Matrix room state query and write helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

if TYPE_CHECKING:
    from .types import HookRoomStatePutter, HookRoomStateQuerier


def build_hook_room_state_querier(
    client: nio.AsyncClient,
) -> HookRoomStateQuerier:
    """Return a querier bound to one Matrix client.

    The returned closure queries room state events via the Matrix client.
    When *state_key* is provided it fetches a single state event; when
    ``None`` it returns all events matching *event_type* as a
    ``{state_key: content}`` dict. Matrix error response objects are
    converted to ``None``; transport exceptions from the client
    propagate to the caller.
    """

    async def _query(
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        if state_key is not None:
            resp = await client.room_get_state_event(room_id, event_type, state_key)
            if isinstance(resp, nio.RoomGetStateEventError):
                return None
            return resp.content

        resp = await client.room_get_state(room_id)
        if isinstance(resp, nio.RoomGetStateError):
            return None
        return {ev["state_key"]: ev["content"] for ev in resp.events if ev.get("type") == event_type}

    return _query


def build_hook_room_state_putter(
    client: nio.AsyncClient,
) -> HookRoomStatePutter:
    """Return a putter bound to one Matrix client.

    The returned closure writes a single room state event via the Matrix
    client and returns ``True`` on success, ``False`` on Matrix error
    response. Transport exceptions from the client propagate to the
    caller.
    """

    async def _put(
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        resp = await client.room_put_state(room_id, event_type, content, state_key=state_key)
        return not isinstance(resp, nio.RoomPutStateError)

    return _put
