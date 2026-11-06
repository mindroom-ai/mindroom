"""Publish the effective reply placement of a room through its managing router."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import is_configured_room, prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.room_thread_modes import resolve_room_thread_mode_override

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_EVENT_TYPE = "io.mindroom.thread_mode"
# Setup, commands, and membership callbacks can reconcile the same room concurrently.
_PUBLICATION_LOCKS: WeakValueDictionary[tuple[Path, str], asyncio.Lock] = WeakValueDictionary()


async def publish_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> None:
    """Reconcile client-facing room state without failing room setup or a command.

    Compare authoritative state rather than a process cache so restarts also avoid
    duplicate events, and failed writes remain eligible for the next reconciliation.
    """
    lock = _PUBLICATION_LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock())
    async with lock:
        try:
            names_by_id = prepared_entity_names_by_user_id(config, runtime_paths)
            if names_by_id.get(client.user_id) != ROUTER_AGENT_NAME:
                return
            snapshot = await read_room_state(client, room_id)
            if snapshot is None:
                return
            names = {
                name
                for user_id, name in names_by_id.items()
                if name != ROUTER_AGENT_NAME
                and snapshot.events.get(("m.room.member", user_id), {}).get("membership") == "join"
            }
            previous = snapshot.events.get((_EVENT_TYPE, ""))
            if not names and previous is None and not is_configured_room(config, room_id, runtime_paths):
                return
            override = resolve_room_thread_mode_override(runtime_paths, room_id)
            modes = {config.get_entity_thread_mode(name, runtime_paths, room_id=room_id) for name in names}
            mode = override or (next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "thread")
            content = {"mode": mode}
            if previous == content:
                return
            response = await client.room_put_state(room_id, _EVENT_TYPE, content, state_key="")
            if not isinstance(response, nio.RoomPutStateResponse):
                logger.warning("room_thread_mode_publish_failed", room_id=room_id, error=str(response))
        except Exception:
            # Presentation metadata must never prevent room setup or handling a turn.
            logger.warning("room_thread_mode_publish_failed", room_id=room_id, exc_info=True)
