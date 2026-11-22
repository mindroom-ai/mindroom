"""A fake homeserver's room state, and the room settings a test gives the runtime without one."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio

from mindroom import room_settings
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.room_settings import MODEL, THREAD_MODE, RoomSetting, seed_room_settings

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.room_settings import RoomThreadMode

ADMIN = "@admin:localhost"


class RoomStateHomeserver:
    """The room's state on a fake homeserver: the admin's power levels and whatever a test or a client sets."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, object]] = {
            ("m.room.power_levels", ""): {"users": {ADMIN: 100}, "users_default": 0},
        }

    def client(self) -> AsyncMock:
        """Return a client whose state reads and writes reach this homeserver."""
        client = AsyncMock()

        async def read(room_id: str, event_type: str, state_key: str = "") -> object:
            content = self.events.get((event_type, state_key))
            if content is None:
                return nio.RoomGetStateEventError("Absent", "M_NOT_FOUND", room_id=room_id)
            return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

        async def write(room_id: str, event_type: str, content: dict[str, object], state_key: str = "") -> object:
            self.events[event_type, state_key] = dict(content)
            return nio.RoomPutStateResponse("$state", room_id)

        client.room_get_state_event.side_effect = read
        client.room_put_state.side_effect = write
        return client


def set_room_setting[T: str](
    setting: RoomSetting[T], runtime_paths: RuntimePaths, room_id: str, value: T | None
) -> None:
    """Give the runtime a room's setting as a read of the room just found it, so no turn reads it again soon."""
    room_settings._known.pop(room_settings._key(setting, runtime_paths, room_id), None)
    content = {} if value is None else {"value": value}
    seed_room_settings(
        runtime_paths,
        room_id,
        [{"type": ROOM_SETTINGS_EVENT_TYPE, "state_key": setting.key, "content": content}],
    )


def set_room_model_override(runtime_paths: RuntimePaths, *, room_id: str, model_name: str) -> None:
    """Give the runtime a room's `model` setting."""
    set_room_setting(MODEL, runtime_paths, room_id, model_name)


def set_room_thread_mode_override(runtime_paths: RuntimePaths, *, room_id: str, mode: RoomThreadMode) -> None:
    """Give the runtime a room's `thread_mode` setting."""
    set_room_setting(THREAD_MODE, runtime_paths, room_id, mode)
