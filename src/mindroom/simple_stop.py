"""Minimal stop button functionality for the bot."""

from __future__ import annotations

import asyncio  # noqa: TC003
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nio import AsyncClient


class SimpleStopManager:
    """Minimal manager for handling stop reactions."""

    def __init__(self) -> None:
        """Initialize the stop manager."""
        self.current_task: asyncio.Task[None] | None = None
        self.current_message_id: str | None = None
        self.current_room_id: str | None = None
        self.current_reaction_event_id: str | None = None

    def set_current(
        self,
        message_id: str,
        room_id: str,
        task: asyncio.Task[None],
        reaction_event_id: str | None = None,
    ) -> None:
        """Set the current generation being processed."""
        self.current_message_id = message_id
        self.current_room_id = room_id
        self.current_task = task
        if reaction_event_id:
            self.current_reaction_event_id = reaction_event_id

    def clear_current(self) -> None:
        """Clear the current generation."""
        self.current_message_id = None
        self.current_room_id = None
        self.current_task = None
        self.current_reaction_event_id = None

    async def handle_stop_reaction(self, message_id: str) -> bool:
        """Handle a stop reaction for a message.

        Returns True if the task was cancelled, False otherwise.
        """
        if self.current_message_id == message_id and self.current_task and not self.current_task.done():
            self.current_task.cancel()
            self.clear_current()
            return True
        return False

    async def add_stop_button(self, client: AsyncClient, room_id: str, message_id: str) -> str | None:
        """Add a stop button reaction to a message.

        Returns:
            The event ID of the reaction if successful, None otherwise.

        """
        try:
            response = await client.room_send(
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
            if hasattr(response, "event_id"):
                return str(response.event_id)
        except Exception:  # noqa: S110
            pass  # Silently ignore reaction failures
        return None

    async def remove_stop_button(self, client: AsyncClient) -> None:
        """Remove the stop button reaction after completion."""
        if self.current_reaction_event_id and self.current_room_id:
            try:
                # Send a redaction event to remove the reaction
                response = await client.room_redact(
                    room_id=self.current_room_id,
                    event_id=self.current_reaction_event_id,
                    reason="Response completed",
                )
                print(f"DEBUG: Redaction response: {response}")
            except Exception as e:
                print(f"DEBUG: Failed to remove reaction: {e}")
