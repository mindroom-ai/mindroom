"""Room state builders for managed rooms and Spaces the router owns."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def router_owned_room_events(
    router_user_id: str,
    room_alias: str,
    *,
    users: Mapping[str, int] | None = None,
) -> list[dict[str, object]]:
    """Return state events for a room the router created under ``room_alias`` and still controls."""
    return [
        {"type": "m.room.create", "state_key": "", "sender": router_user_id, "content": {}},
        {
            "type": "m.room.canonical_alias",
            "state_key": "",
            "sender": router_user_id,
            "content": {"alias": room_alias},
        },
        {
            "type": "m.room.member",
            "state_key": router_user_id,
            "sender": router_user_id,
            "content": {"membership": "join"},
        },
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "sender": router_user_id,
            "content": {"users": {router_user_id: 100, **(users or {})}, "users_default": 0, "state_default": 50},
        },
    ]
