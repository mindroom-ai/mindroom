"""Internal helper for bounded Matrix room reads through a room's steward bot."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.logging_config import get_logger
from mindroom.room_steward import room_steward

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    from fastapi import FastAPI

# A client can tell the condition that lasts until a bot joins the room from a failure worth retrying.
_NO_BOT_IN_ROOM = "No running MindRoom bot is in this room."
# As long as the computers API waits for the same membership check.
_ROOM_READ_TIMEOUT_SECONDS = 20
logger = get_logger(__name__)


async def read_in_room[T](
    app: FastAPI,
    room_id: str,
    read: Callable[[nio.AsyncClient], Awaitable[T]],
    failed: str,
) -> T:
    """Return ``read`` made through the client of the room's steward, else answer 503 with ``failed``.

    It answers 503 too when no running bot is in the room, or when ``read`` raises or outlasts the lookup timeout.
    """
    bots = ensure_app_state(app).room_bots
    steward = None if bots is None else room_steward(bots, room_id)
    client = None if steward is None else steward.client
    if client is None:
        raise HTTPException(503, _NO_BOT_IN_ROOM)
    try:
        async with asyncio.timeout(_ROOM_READ_TIMEOUT_SECONDS):
            return await read(client)
    except Exception as error:
        # A failed or timed-out read leaves the answer unknown, which must never pass for a yes or a no.
        logger.warning("Room read failed", room_id=room_id, detail=failed, error_type=type(error).__name__)
        raise HTTPException(503, failed) from None
