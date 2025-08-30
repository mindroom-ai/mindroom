"""Typing indicator management for Matrix agents."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)


async def set_typing(
    client: nio.AsyncClient,
    room_id: str,
    typing: bool = True,
    timeout_ms: int = 30000,
) -> None:
    """Set typing status for a user in a room.

    Args:
        client: Matrix client instance
        room_id: Room to show typing indicator in
        typing: Whether to show or hide typing indicator
        timeout_ms: How long the typing indicator should last (in milliseconds)

    """
    response = await client.room_typing(room_id, typing, timeout_ms)
    if isinstance(response, nio.RoomTypingError):
        logger.warning(
            "Failed to set typing status",
            room_id=room_id,
            typing=typing,
            error=response.message,
        )
    else:
        logger.debug("Set typing status", room_id=room_id, typing=typing)


@asynccontextmanager
async def typing_indicator(
    client: nio.AsyncClient,
    room_id: str,
    timeout_ms: int = 30000,
) -> AsyncGenerator[None, None]:
    """Context manager for showing typing indicator while processing.

    Usage:
        async with typing_indicator(client, room_id):
            # Do work here - typing indicator shown
            response = await generate_response()
        # Typing indicator automatically stopped

    Args:
        client: Matrix client instance
        room_id: Room to show typing indicator in
        timeout_ms: How long each typing notification lasts

    """
    # Start typing
    await set_typing(client, room_id, True, timeout_ms)

    # Create a task to periodically refresh the typing indicator
    # Matrix typing indicators expire, so we need to refresh them
    refresh_interval = min(timeout_ms / 2, 15000) / 1000  # Convert to seconds

    async def refresh_typing() -> None:
        """Refresh typing indicator periodically."""
        while True:
            await asyncio.sleep(refresh_interval)
            await set_typing(client, room_id, True, timeout_ms)

    refresh_task = asyncio.create_task(refresh_typing())

    try:
        yield
    finally:
        # Cancel refresh task
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task

        # Stop typing
        await set_typing(client, room_id, False)
