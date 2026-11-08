"""Change a room's thread mode override along the one path `!thread_mode` and plugin routes take."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.room_thread_modes import clear_room_thread_mode_override, set_room_thread_mode_override

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.room_thread_modes import RoomThreadMode


async def change_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: RoomThreadMode | None,
) -> bool:
    """Store the room's thread mode override, or clear it given None, then republish the mode through the router's client.

    The republish reads the override again under its per-room lock, so changes made at once leave the published state
    matching the override stored last. Return False only when there was no override to clear.
    """
    if mode is None:
        changed = clear_room_thread_mode_override(runtime_paths, room_id)
    else:
        set_room_thread_mode_override(runtime_paths, room_id=room_id, mode=mode)
        changed = True
    await reconcile_room_thread_mode(client, config, runtime_paths, room_id)
    return changed
