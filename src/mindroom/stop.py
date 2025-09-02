"""Minimal stop button functionality for the bot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

if TYPE_CHECKING:
    from nio import AsyncClient

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TrackedMessage:
    """Track a message with stop button."""

    message_id: str
    room_id: str
    task: asyncio.Task[None]
    reaction_event_id: str | None = None


class StopManager:
    """Manager for handling stop reactions."""

    def __init__(self) -> None:
        """Initialize the stop manager."""
        # Track multiple concurrent messages by message_id
        self.tracked_messages: dict[str, TrackedMessage] = {}
        # Keep references to cleanup tasks
        self.cleanup_tasks: list[asyncio.Task] = []
        logger.info("StopManager initialized")

    def set_current(
        self,
        message_id: str,
        room_id: str,
        task: asyncio.Task[None],
        reaction_event_id: str | None = None,
    ) -> None:
        """Track a message generation."""
        self.tracked_messages[message_id] = TrackedMessage(
            message_id=message_id,
            room_id=room_id,
            task=task,
            reaction_event_id=reaction_event_id,
        )
        logger.info(
            "Tracking message generation",
            message_id=message_id,
            room_id=room_id,
            reaction_event_id=reaction_event_id,
            total_tracked=len(self.tracked_messages),
        )

    def clear_message(
        self,
        message_id: str,
        client: AsyncClient,
        remove_button: bool = True,
        delay: float = 5.0,
    ) -> None:
        """Clear tracking for a specific message and optionally remove stop button.

        Args:
            message_id: The message ID to clear
            client: Matrix client for removing stop button
            remove_button: Whether to remove the stop button (default True)
            delay: Seconds to wait before clearing (default 5.0)

        """

        async def delayed_clear() -> None:
            """Clear the message and remove stop button after a delay."""
            if remove_button and message_id in self.tracked_messages:
                tracked = self.tracked_messages[message_id]
                if tracked.reaction_event_id:
                    logger.info("Removing stop button in cleanup", message_id=message_id)
                    try:
                        await client.room_redact(
                            room_id=tracked.room_id,
                            event_id=tracked.reaction_event_id,
                            reason="Response completed",
                        )
                        tracked.reaction_event_id = None
                    except Exception as e:
                        logger.warning(f"Failed to remove stop button in cleanup: {e}")

            await asyncio.sleep(delay)
            if message_id in self.tracked_messages:
                logger.info("Clearing tracked message after delay", message_id=message_id, delay=delay)
                del self.tracked_messages[message_id]

        if message_id in self.tracked_messages:
            logger.info(
                "Scheduling message cleanup",
                message_id=message_id,
                delay=delay,
                remove_button=remove_button,
            )
            task = asyncio.create_task(delayed_clear())
            self.cleanup_tasks.append(task)
            # Clean up old completed tasks
            self.cleanup_tasks = [t for t in self.cleanup_tasks if not t.done()]
        else:
            logger.debug("Message not tracked, skipping cleanup", message_id=message_id)

    async def handle_stop_reaction(self, message_id: str) -> bool:
        """Handle a stop reaction for a message.

        Returns True if the task was cancelled, False otherwise.
        """
        logger.info(
            "Handling stop reaction",
            message_id=message_id,
            tracked_messages=list(self.tracked_messages.keys()),
        )

        if message_id in self.tracked_messages:
            tracked = self.tracked_messages[message_id]
            if tracked.task and not tracked.task.done():
                logger.info("Cancelling task for message", message_id=message_id)
                tracked.task.cancel()
                # Don't clear here - let the finally block handle it
                return True
            logger.info(
                "Task already completed or missing",
                message_id=message_id,
                task_exists=tracked.task is not None,
                task_done=tracked.task.done() if tracked.task else None,
            )
        else:
            logger.warning("Stop reaction for untracked message", message_id=message_id)
        return False

    async def add_stop_button(self, client: AsyncClient, room_id: str, message_id: str) -> str | None:
        """Add a stop button reaction to a message.

        Returns:
            The event ID of the reaction if successful, None otherwise.

        """
        logger.info("Adding stop button", room_id=room_id, message_id=message_id)
        try:
            response = await client.room_send(
                room_id=room_id,
                message_type="m.reaction",
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": message_id,
                        "key": "🛑",
                    },
                },
            )
            if isinstance(response, nio.RoomSendResponse):
                event_id = str(response.event_id)
                logger.info("Stop button added successfully", reaction_event_id=event_id, message_id=message_id)
                # Update the tracked message with the reaction event ID
                if message_id in self.tracked_messages:
                    self.tracked_messages[message_id].reaction_event_id = event_id
                return event_id
            logger.warning("Failed to add stop button - no event_id in response", response=response)
        except Exception as e:
            logger.exception("Exception adding stop button", error=str(e))
        return None

    async def remove_stop_button(self, client: AsyncClient, message_id: str | None = None) -> None:
        """Remove the stop button reaction immediately when user clicks it.

        Args:
            client: The Matrix client
            message_id: The message ID to remove the button from

        """
        if message_id and message_id in self.tracked_messages:
            tracked = self.tracked_messages[message_id]
            if tracked.reaction_event_id and tracked.room_id:
                logger.info(
                    "Removing stop button immediately (user clicked)",
                    message_id=message_id,
                    reaction_event_id=tracked.reaction_event_id,
                )
                try:
                    await client.room_redact(
                        room_id=tracked.room_id,
                        event_id=tracked.reaction_event_id,
                        reason="User clicked stop",
                    )
                    tracked.reaction_event_id = None
                    logger.info("Stop button removed successfully")
                except Exception as e:
                    logger.exception("Failed to remove stop button", error=str(e))
            else:
                logger.debug(
                    "Stop button already removed or missing",
                    message_id=message_id,
                    has_reaction_id=tracked.reaction_event_id is not None,
                )
        else:
            logger.debug("Message not tracked, cannot remove stop button", message_id=message_id)
