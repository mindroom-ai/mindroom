"""Live room reads for plugin HTTP routes, made through the orchestrator's running bots."""

from __future__ import annotations

import asyncio
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING

import nio
from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.matrix.history_visibility import read_room_history
from mindroom.matrix.joined_members import read_joined_members
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.rooms import is_dm_room
from mindroom.room_steward import read_in_room

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from mindroom.bot import AgentBot
    from mindroom.matrix.history_visibility import RoomHistory
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot

_LOOKUP_FAILED = "Room membership lookup failed; try again."
_STATE_READ_FAILED = "Room state read failed; try again."
_DIRECT_LOOKUP_FAILED = "Direct message lookup failed; try again."
_HISTORY_READ_FAILED = "Room history read failed; try again."


def bind_room_members(app: FastAPI, bots: Mapping[str, AgentBot]) -> None:
    """Publish readers of a room's joined users and names, its state and history, and whether it is a direct message.

    Each read is answered by the room's steward. ``bots`` must be the orchestrator's live mapping, read at each
    lookup, so a bot that stopped, left the room, or restarted with a new client counts as it is now, and one binding
    lasts for as long as the API serves.
    Callers that ask about a room's members while its lookup runs share that lookup, its answer or its failure, so a
    burst of requests makes one Matrix call; no answer is kept once its lookup ends, so a later caller asks again.
    """
    lookups: dict[str, asyncio.Task[Mapping[str, str | None]]] = {}

    async def look_up(room_id: str) -> Mapping[str, str | None]:
        response = await read_in_room(
            bots,
            room_id,
            lambda client: read_joined_members(client, room_id),
            _LOOKUP_FAILED,
        )
        if not isinstance(response, nio.JoinedMembersResponse):
            raise HTTPException(503, _LOOKUP_FAILED)
        # Read-only, since every caller sharing the lookup gets this one mapping.
        return MappingProxyType({member.user_id: member.display_name or None for member in response.members})

    def forget(room_id: str, lookup: asyncio.Task[Mapping[str, str | None]]) -> None:
        if lookups.get(room_id) is lookup:
            del lookups[room_id]
        if not lookup.cancelled():
            lookup.exception()  # Retrieved, so a lookup that fails after all its callers went away logs no error.

    async def room_members(room_id: str) -> Mapping[str, str | None]:
        lookup = lookups.get(room_id)
        # A finished lookup is never joined, even before its callback removes it.
        if lookup is None or lookup.done():
            lookup = lookups[room_id] = asyncio.create_task(look_up(room_id))
            lookup.add_done_callback(lambda done: forget(room_id, done))
        # Waiting never cancels the lookup, so a caller that goes away leaves it running for the others.
        await asyncio.wait([lookup])
        return lookup.result()

    state = ensure_app_state(app)
    state.room_members = room_members
    state.room_state = partial(_room_state, bots)
    state.room_direct = partial(_room_direct, bots)
    state.room_history = partial(_room_history, bots)
    state.room_bots = bots


async def _room_state(bots: Mapping[str, AgentBot], room_id: str) -> RoomStateSnapshot:
    state = await read_in_room(bots, room_id, lambda client: read_room_state(client, room_id), _STATE_READ_FAILED)
    if state is None:
        raise HTTPException(503, _STATE_READ_FAILED)
    return state


async def _room_direct(bots: Mapping[str, AgentBot], room_id: str) -> bool:
    direct = await read_in_room(bots, room_id, lambda client: is_dm_room(client, room_id), _DIRECT_LOOKUP_FAILED)
    if direct is None:
        raise HTTPException(503, _DIRECT_LOOKUP_FAILED)
    return direct


async def _room_history(bots: Mapping[str, AgentBot], room_id: str, user_id: str) -> RoomHistory | None:
    return await read_in_room(
        bots,
        room_id,
        lambda client: read_room_history(client, room_id, user_id),
        _HISTORY_READ_FAILED,
    )


def unbind_room_members(app: FastAPI) -> None:
    """Withdraw the readers once the API stops serving."""
    state = ensure_app_state(app)
    state.room_members = None
    state.room_state = None
    state.room_direct = None
    state.room_history = None
    state.room_bots = None
