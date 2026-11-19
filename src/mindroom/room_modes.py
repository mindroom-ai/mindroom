"""Change a room's thread mode override along the one path `!thread_mode` and plugin routes take."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.room_thread_modes import (
    clear_room_thread_mode_override,
    resolve_room_thread_mode_override,
    set_room_thread_mode_override,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.room_thread_modes import RoomThreadMode

_CHANGES: dict[tuple[Path, str], asyncio.Lock] = {}


async def change_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: RoomThreadMode | None,
    *,
    put_state: Callable[..., Awaitable[bool | None]] | None = None,
) -> bool | None:
    """Store the room's thread mode override, or clear it given None, then republish the mode.

    The mode is written through the given client, or through ``put_state`` for a client that only answers.

    The republish reads the override again under its per-room lock, so changes made at once leave the published state
    matching the override stored last. Return whether the room's state shows the result; the override stays stored
    either way, except that a refused write, None, restores the override the room's clients show.
    """

    def store(stored: RoomThreadMode | None) -> None:
        if stored is None:
            clear_room_thread_mode_override(runtime_paths, room_id)
        else:
            set_room_thread_mode_override(runtime_paths, room_id=room_id, mode=stored)

    # One change at a time per room, so a refused change restores the override no other change has replaced since.
    async with _CHANGES.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock()):
        previous = resolve_room_thread_mode_override(runtime_paths, room_id)
        store(mode)
        current = await reconcile_room_thread_mode(client, config, runtime_paths, room_id, put_state=put_state)
        if current is None:
            store(previous)
        return current
