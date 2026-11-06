"""Publish the effective reply placement of a room through its managing router."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import (
    configured_routable_entity_names_for_room,
    is_configured_room,
    prepared_entity_names_by_user_id,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import RoomStateSnapshot, read_room_state
from mindroom.room_thread_modes import resolve_room_thread_mode_override

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_EVENT_TYPE = "io.mindroom.thread_mode"
_PUBLICATION_LOCKS: WeakValueDictionary[tuple[Path, str], asyncio.Lock] = WeakValueDictionary()


@dataclass
class _RoomModeState:
    """A seeded room roster and its last successfully published mode."""

    joined_user_ids: set[str]
    aliases: tuple[str, ...]
    published_mode: str | None


@lru_cache(maxsize=64)
def _room_states(_storage_root: Path) -> dict[str, _RoomModeState]:
    """Keep independent bounded runtime caches across config generations."""
    return {}


def seed_room_thread_mode_state(runtime_paths: RuntimePaths, snapshot: RoomStateSnapshot) -> None:
    """Reuse the authoritative policy read before bots reconcile their memberships."""
    alias = snapshot.events.get(("m.room.canonical_alias", ""), {}).get("alias")
    mode = snapshot.events.get((_EVENT_TYPE, ""), {}).get("mode")
    state = _RoomModeState(
        joined_user_ids={
            user_id
            for (event_type, user_id), content in snapshot.events.items()
            if event_type == "m.room.member" and content.get("membership") == "join"
        },
        aliases=(alias,) if isinstance(alias, str) and alias else (),
        published_mode=mode if isinstance(mode, str) else None,
    )
    _room_states(runtime_paths.storage_root).setdefault(snapshot.room_id, state)


async def initialize_room_thread_mode_state(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> None:
    """Read an unseeded room once when setup has no reusable policy snapshot."""
    if room_id not in _room_states(runtime_paths.storage_root):
        snapshot = await read_room_state(client, room_id)
        if snapshot is not None:
            seed_room_thread_mode_state(runtime_paths, snapshot)


def record_room_thread_mode_membership(
    runtime_paths: RuntimePaths,
    room_id: str,
    user_id: str,
    membership: str,
) -> None:
    """Keep a seeded roster current from confirmed local commands and router member events."""
    state = _room_states(runtime_paths.storage_root).get(room_id)
    if state is None:
        return
    if membership == "join":
        state.joined_user_ids.add(user_id)
    else:
        state.joined_user_ids.discard(user_id)


async def publish_room_thread_mode(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    allow_read: bool = True,
) -> None:
    """Publish changed modes, reading state only to initialize an unseeded room.

    Failed writes leave the previous published mode intact so reconciliation retries.
    Reloads use only seeded rosters and never fetch Matrix state.
    """
    lock = _PUBLICATION_LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock())
    async with lock:
        try:
            names_by_id = prepared_entity_names_by_user_id(config, runtime_paths)
            if names_by_id.get(client.user_id) != ROUTER_AGENT_NAME:
                return
            states = _room_states(runtime_paths.storage_root)
            if room_id not in states:
                if not allow_read:
                    return
                await initialize_room_thread_mode_state(client, runtime_paths, room_id)
                if room_id not in states:
                    return
            state = states[room_id]
            names = {
                name
                for user_id, name in names_by_id.items()
                if name != ROUTER_AGENT_NAME and user_id in state.joined_user_ids
            }
            configured_names = configured_routable_entity_names_for_room(
                config,
                room_id,
                runtime_paths,
                room_aliases=state.aliases,
            )
            # Routing uses configured responders when present, even if other agents joined by invitation.
            if configured_names:
                names.intersection_update(configured_names)
            if (
                not names
                and state.published_mode is None
                and not is_configured_room(
                    config,
                    room_id,
                    runtime_paths,
                    room_aliases=state.aliases,
                )
            ):
                return
            override = resolve_room_thread_mode_override(runtime_paths, room_id)
            modes = {config.get_entity_thread_mode(name, runtime_paths, room_id=room_id) for name in names}
            mode = override or (next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "thread")
            if state.published_mode == mode:
                return
            response = await client.room_put_state(room_id, _EVENT_TYPE, {"mode": mode}, state_key="")
            if isinstance(response, nio.RoomPutStateResponse):
                state.published_mode = mode
            else:
                logger.warning("room_thread_mode_publish_failed", room_id=room_id, error=str(response))
        except Exception:
            logger.warning("room_thread_mode_publish_failed", room_id=room_id, exc_info=True)
