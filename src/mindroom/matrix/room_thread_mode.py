"""Compute effective room modes and publish router-owned presentation state."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import configured_routable_entity_names_for_room, prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.room_membership import cached_joined_member_ids
from mindroom.matrix.room_reconciliation import RoomStateSnapshot
from mindroom.room_thread_modes import resolve_room_thread_mode_override

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from pathlib import Path
    from typing import Literal

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
THREAD_MODE_EVENT_TYPE = "io.mindroom.thread_mode"
_IN_FLIGHT: dict[tuple[Path, str], tuple[str, asyncio.Task[None]]] = {}


def effective_room_thread_mode(
    config: Config,
    runtime_paths: RuntimePaths,
    room: nio.MatrixRoom | RoomStateSnapshot,
    *,
    joined_user_ids: AbstractSet[str] | None = None,
) -> Literal["room", "thread", "mixed"]:
    """Resolve eligible responders from supplied state, without fetching or retaining remote state."""
    override = resolve_room_thread_mode_override(runtime_paths, room.room_id)
    if override is not None:
        return override
    if isinstance(room, RoomStateSnapshot):
        members = {
            key
            for (kind, key), content in room.events.items()
            if kind == "m.room.member" and content.get("membership") == "join"
        }
        alias = room.events.get(("m.room.canonical_alias", ""), {}).get("alias")
    else:
        members = cached_joined_member_ids(room)
        alias = room.canonical_alias
    aliases = (alias,) if isinstance(alias, str) and alias else ()
    names = {
        name
        for user_id, name in prepared_entity_names_by_user_id(config, runtime_paths).items()
        if name != ROUTER_AGENT_NAME and user_id in (members if joined_user_ids is None else joined_user_ids)
    }
    configured = configured_routable_entity_names_for_room(config, room.room_id, runtime_paths, room_aliases=aliases)
    if configured:
        names.intersection_update(configured)
    modes = {config.get_entity_thread_mode(name, runtime_paths, room_id=room.room_id) for name in names}
    return "mixed" if len(modes) > 1 else next(iter(modes), "thread")


async def publish_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: str | None = None,
) -> None:
    """Serialize writes and share identical in-flight sends, forgetting them once they finish."""

    async def send(previous: asyncio.Task[None] | None) -> None:
        try:
            if previous is not None:
                await asyncio.shield(previous)
            response = await client.room_put_state(room_id, THREAD_MODE_EVENT_TYPE, {"mode": mode}, state_key="")
            if not isinstance(response, nio.RoomPutStateResponse):
                logger.warning("room_thread_mode_publish_failed", room_id=room_id, error=str(response))
        except Exception:
            logger.warning("room_thread_mode_publish_failed", room_id=room_id, exc_info=True)
        finally:
            if _IN_FLIGHT.get(key) == (mode, asyncio.current_task()):
                del _IN_FLIGHT[key]

    try:
        if prepared_entity_names_by_user_id(config, runtime_paths).get(client.user_id) != ROUTER_AGENT_NAME:
            return
        if mode is None:
            room = client.rooms.get(room_id, nio.MatrixRoom(room_id, client.user_id or ""))
            mode = effective_room_thread_mode(config, runtime_paths, room)
        key = (runtime_paths.storage_root, room_id)
        pending = _IN_FLIGHT.get(key)
        if pending is None or pending[0] != mode or pending[1].done():
            task = asyncio.create_task(send(pending[1] if pending else None))
            _IN_FLIGHT[key] = (mode, task)
        else:
            task = pending[1]
        await asyncio.shield(task)
    except Exception:
        logger.warning("room_thread_mode_publish_failed", room_id=room_id, exc_info=True)
