"""Minimal stop button functionality for the bot."""

from __future__ import annotations

import asyncio  # noqa: TC003
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nio import AsyncClient


class SimpleStopManager:
    """Minimal manager for handling stop reactions."""

    def __init__(self) -> None:
        """Initialize the stop manager."""
        self.current_task: asyncio.Task[None] | None = None
        self.current_message_id: str | None = None

    def set_current(self, message_id: str, task: asyncio.Task[None]) -> None:
        """Set the current generation being processed."""
        self.current_message_id = message_id
        self.current_task = task

    def clear_current(self) -> None:
        """Clear the current generation."""
        self.current_message_id = None
        self.current_task = None

    async def handle_stop_reaction(self, message_id: str) -> bool:
        """Handle a stop reaction for a message.

        Returns True if the task was cancelled, False otherwise.
        """
        if self.current_message_id == message_id and self.current_task and not self.current_task.done():
            self.current_task.cancel()
            self.clear_current()
            return True
        return False

    async def add_stop_button(self, client: AsyncClient, room_id: str, message_id: str) -> None:
        """Add a stop button reaction to a message."""
        with suppress(Exception):
            await client.room_send(
                room_id=room_id,
                message_type="m.reaction",
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": message_id,
                        "key": "❌",
                    },
                },
            )
