"""Orchestrator-owned live room membership for plugin HTTP routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fastapi import FastAPI

    from mindroom.bot import AgentBot

logger = get_logger(__name__)

_UNAVAILABLE = "Room membership is unavailable."


def _bind_room_members(
    app: FastAPI,
    router_client: nio.AsyncClient,
    *,
    live_client: Callable[[], nio.AsyncClient | None] | None = None,
) -> None:
    """Publish a reader of a room's joined users, answered by the router bot's client.

    ``live_client`` returns the client to use right now, or None when it is gone; it defaults to ``router_client``.
    """

    async def room_members(room_id: str) -> frozenset[str]:
        client = router_client if live_client is None else live_client()
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
    """Bind on the API app while the router bot runs with a client; unbind otherwise."""
    if not api_enabled:
        return
    from mindroom.api import main  # noqa: PLC0415

    router_bot = bots.get(ROUTER_AGENT_NAME)
    if router_bot is None or router_bot.client is None or not router_bot.running:
        _unbind_room_members(main.app)
        return
    _bind_room_members(
        main.app,
        router_bot.client,
        live_client=lambda: router_bot.client if router_bot.running else None,
    )


def withdraw_room_members(*, api_enabled: bool) -> None:
    """Unbind from the API app before entity reload or orchestrator shutdown."""
    if not api_enabled:
        return
    from mindroom.api import main  # noqa: PLC0415

    _unbind_room_members(main.app)


def _unbind_room_members(app: FastAPI) -> None:
    """Withdraw the reader before the router bot's client goes away."""
    ensure_app_state(app).room_members = None
