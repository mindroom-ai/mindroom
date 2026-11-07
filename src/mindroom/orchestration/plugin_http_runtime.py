"""Live room membership for plugin HTTP routes, read through the orchestrator's running bots."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio
from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from mindroom.bot import AgentBot

logger = get_logger(__name__)

# A client can tell the condition that lasts until a bot joins the room from a failure worth retrying.
_NO_BOT_IN_ROOM = "No running MindRoom bot is in this room."
_LOOKUP_FAILED = "Room membership lookup failed; try again."
# As long as the computers API waits for the same membership check.
_LOOKUP_TIMEOUT_SECONDS = 20


def _client_in_room(bots: Mapping[str, AgentBot], room_id: str) -> nio.AsyncClient | None:
    """Return the router's live client if it runs and is joined to the room, else any running bot's that is."""
    router = bots.get(ROUTER_AGENT_NAME)
    others = (bot for name, bot in bots.items() if name != ROUTER_AGENT_NAME)
    for bot in (router, *others):
        if bot is None or not bot.running:
            continue
        client = bot.client
        if client is not None and room_id in client.rooms:
            return client
    return None


def bind_room_members(app: FastAPI, bots: Mapping[str, AgentBot]) -> None:
    """Publish a reader of a room's joined users, answered by a running bot in that room, the router first.

    ``bots`` must be the orchestrator's live mapping, read at each call, so a bot that stopped, left the room, or
    restarted with a new client counts as it is now, and one binding lasts for as long as the API serves.
    Callers that ask about a room while its lookup runs share that lookup, its answer or its failure, so a burst of
    requests makes one Matrix call; no answer is kept once its lookup ends, so a later caller asks again.
    """
    lookups: dict[str, asyncio.Task[frozenset[str]]] = {}

    async def look_up(room_id: str) -> frozenset[str]:
        client = _client_in_room(bots, room_id)
        if client is None:
            raise HTTPException(503, _NO_BOT_IN_ROOM)
        try:
            async with asyncio.timeout(_LOOKUP_TIMEOUT_SECONDS):
                response = await client.joined_members(room_id)
        except Exception as error:
            # A failed or timed-out lookup leaves membership unknown, which must never let the caller through.
            logger.warning("Room membership lookup failed", room_id=room_id, error_type=type(error).__name__)
            raise HTTPException(503, _LOOKUP_FAILED) from None
        if not isinstance(response, nio.JoinedMembersResponse):
            raise HTTPException(503, _LOOKUP_FAILED)
        return frozenset(member.user_id for member in response.members)

    def forget(room_id: str, lookup: asyncio.Task[frozenset[str]]) -> None:
        del lookups[room_id]
        if not lookup.cancelled():
            lookup.exception()  # Retrieved, so a lookup that fails after all its callers went away logs no error.

    async def room_members(room_id: str) -> frozenset[str]:
        lookup = lookups.get(room_id)
        if lookup is None:
            lookup = lookups[room_id] = asyncio.create_task(look_up(room_id))
            lookup.add_done_callback(lambda done: forget(room_id, done))
        # Shielded, so a caller that goes away leaves the lookup running for the others.
        return await asyncio.shield(lookup)

    ensure_app_state(app).room_members = room_members


def unbind_room_members(app: FastAPI) -> None:
    """Withdraw the reader once the API stops serving."""
    ensure_app_state(app).room_members = None
