"""Orchestrator-owned live room membership for plugin HTTP routes."""

from __future__ import annotations

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

_UNAVAILABLE = "Room membership is unavailable."


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


def _bind_room_members(app: FastAPI, bots: Mapping[str, AgentBot]) -> None:
    """Publish a reader of a room's joined users, answered by a running bot in that room, the router first.

    ``bots`` is read at each call, so a bot that stopped, left the room, or restarted with a new client counts as it is now.
    """

    async def room_members(room_id: str) -> frozenset[str]:
        client = _client_in_room(bots, room_id)
        if client is None:
            raise HTTPException(503, _UNAVAILABLE)
        try:
            response = await client.joined_members(room_id)
        except Exception as error:
            # A failed lookup leaves membership unknown, which must never let the caller through.
            logger.warning("Room membership lookup failed", room_id=room_id, error_type=type(error).__name__)
            raise HTTPException(503, _UNAVAILABLE) from None
        if not isinstance(response, nio.JoinedMembersResponse):
            raise HTTPException(503, _UNAVAILABLE)
        return frozenset(member.user_id for member in response.members)

    ensure_app_state(app).room_members = room_members


def bind_room_members_if_ready(bots: Mapping[str, AgentBot], *, api_enabled: bool) -> None:
    """Bind on the API app while any bot runs with a client; unbind otherwise.

    ``bots`` must be the orchestrator's live mapping: the reader picks the bot to ask at each call.
    """
    if not api_enabled:
        return
    from mindroom.api import main  # noqa: PLC0415

    if not any(bot.running and bot.client is not None for bot in bots.values()):
        _unbind_room_members(main.app)
        return
    _bind_room_members(main.app, bots)


def withdraw_room_members(*, api_enabled: bool) -> None:
    """Unbind from the API app before entity reload or orchestrator shutdown."""
    if not api_enabled:
        return
    from mindroom.api import main  # noqa: PLC0415

    _unbind_room_members(main.app)


def _unbind_room_members(app: FastAPI) -> None:
    """Withdraw the reader before the bots' clients go away."""
    ensure_app_state(app).room_members = None
