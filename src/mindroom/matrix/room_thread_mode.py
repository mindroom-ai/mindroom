"""Reconcile the router-owned thread-mode state clients use to choose a room view."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.authorization import configured_responder_entities_for_room, get_available_responders_in_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.room_membership import ensure_room_membership_synced
from mindroom.room_thread_modes import resolve_room_thread_mode_override

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_THREAD_MODE_EVENT_TYPE = "io.mindroom.thread_mode"
_LOCKS: WeakValueDictionary[tuple[Path, str], asyncio.Lock] = WeakValueDictionary()


async def reconcile_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> None:
    """Compare one state event with reply-path responders; failures leave the next trigger free to retry."""
    try:
        identities = prepared_entity_names_by_user_id(config, runtime_paths)
        if identities.get(client.user_id) != ROUTER_AGENT_NAME:
            return
        # The deadline covers the lock wait too, so a sync frame queued behind other triggers stalls at most 10 s.
        async with asyncio.timeout(10):
            # Serialize the comparison and write so concurrent triggers cannot send duplicate or stale values.
            async with _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock()):
                room = client.rooms[room_id]
                responders = configured_responder_entities_for_room(room, config, runtime_paths)
                if responders is None:
                    if not await ensure_room_membership_synced(client, room, sender_id=client.user_id):
                        logger.debug("room_thread_mode_membership_unavailable", room_id=room_id)
                        return
                    responders = get_available_responders_in_room(room, config, runtime_paths)
                modes = {
                    config.get_entity_thread_mode(identities[responder.full_id], runtime_paths, room_id=room_id)
                    for responder in responders
                    if await _joined(client, room_id, responder.full_id)
                }
                if not modes:
                    return
                mode = resolve_room_thread_mode_override(runtime_paths, room_id) or (
                    "mixed" if len(modes) > 1 else next(iter(modes))
                )
                content = {"mode": mode}
                current = await client.room_get_state_event(room_id, _THREAD_MODE_EVENT_TYPE, state_key="")
                if isinstance(current, nio.RoomGetStateEventResponse) and current.content == content:
                    return
                if isinstance(current, nio.RoomGetStateEventError) and current.status_code != "M_NOT_FOUND":
                    logger.warning("room_thread_mode_reconcile_failed", room_id=room_id, error=str(current))
                    return
                response = await client.room_put_state(room_id, _THREAD_MODE_EVENT_TYPE, content, state_key="")
                if not isinstance(response, nio.RoomPutStateResponse):
                    logger.warning("room_thread_mode_reconcile_failed", room_id=room_id, error=str(response))
    except Exception:
        logger.warning("room_thread_mode_reconcile_failed", room_id=room_id, exc_info=True)


async def _joined(client: nio.AsyncClient, room_id: str, user_id: str) -> bool:
    membership = await client.room_get_state_event(room_id, "m.room.member", user_id)
    if isinstance(membership, nio.RoomGetStateEventError) and membership.status_code != "M_NOT_FOUND":
        raise RuntimeError(str(membership))
    return isinstance(membership, nio.RoomGetStateEventResponse) and membership.content.get("membership") == "join"
