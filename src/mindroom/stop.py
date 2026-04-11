"""Stop button tracking with hard-cancel-first response handling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
from agno.run.cancel import acancel_run

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from nio import AsyncClient

logger = get_logger(__name__)
_GRACEFUL_CANCEL_FALLBACK_SECONDS = 10.0
_GRACEFUL_CANCEL_PROBE_SECONDS = 0.25


@dataclass
class _TrackedMessage:
    """Track a message with stop button."""

    message_id: str
    room_id: str
    task: asyncio.Task[None]
    thread_id: str | None = None
    reaction_event_id: str | None = None
    run_id: str | None = None
    cancel_requested: bool = False


class StopManager:
    """Manage stop reactions with immediate task cancellation."""

    def __init__(self, graceful_cancel_fallback_seconds: float = _GRACEFUL_CANCEL_FALLBACK_SECONDS) -> None:
        """Initialize the stop manager."""
        # Track multiple concurrent messages by message_id
        self.tracked_messages: dict[str, _TrackedMessage] = {}
        # Keep references to cleanup tasks
        self.cleanup_tasks: list[asyncio.Task[None]] = []
        self.graceful_cancel_fallback_seconds = graceful_cancel_fallback_seconds
        logger.info("StopManager initialized")

    def set_current(
        self,
        message_id: str,
        room_id: str,
        task: asyncio.Task[None],
        reaction_event_id: str | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Track a message generation."""
        self.tracked_messages[message_id] = _TrackedMessage(
            message_id=message_id,
            room_id=room_id,
            task=task,
            thread_id=thread_id,
            reaction_event_id=reaction_event_id,
            run_id=run_id,
        )
        logger.info(
            "Tracking message generation",
            message_id=message_id,
            room_id=room_id,
            thread_id=thread_id,
            reaction_event_id=reaction_event_id,
            run_id=run_id,
            total_tracked=len(self.tracked_messages),
        )

    def update_run_id(self, message_id: str | None, run_id: str | None) -> None:
        """Update the tracked Agno run_id for a message before a new attempt starts."""
        if message_id is None:
            return

        tracked = self._get_active_tracked_message(message_id)
        if tracked is None or tracked.run_id == run_id:
            return

        previous_run_id = tracked.run_id
        tracked.run_id = run_id
        logger.info(
            "Updated tracked run id",
            message_id=message_id,
            thread_id=tracked.thread_id,
            previous_run_id=previous_run_id,
            run_id=run_id,
            cancel_requested=tracked.cancel_requested,
        )

        if tracked.cancel_requested and run_id:
            logger.info(
                "Stop already requested; scheduling best-effort cleanup for updated run id",
                message_id=message_id,
                thread_id=tracked.thread_id,
                run_id=run_id,
            )
            self._schedule_graceful_run_cancel(message_id, run_id)

    def _discard_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Drop finished background tasks from the strong-reference list."""
        with suppress(ValueError):
            self.cleanup_tasks.remove(task)

    def _track_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Keep a strong reference to background cleanup/fallback tasks."""
        task.add_done_callback(self._discard_cleanup_task)
        self.cleanup_tasks.append(task)

    def _get_active_tracked_message(self, message_id: str) -> _TrackedMessage | None:
        """Return the tracked message while its task is still active."""
        tracked = self.tracked_messages.get(message_id)
        if tracked is None or tracked.task.done():
            return None
        return tracked

    async def _probe_graceful_cancel(self, message_id: str, run_id: str, deadline: float) -> str:
        """Request Agno run cancellation for one known run during the post-cancel probe window."""
        tracked = self.tracked_messages.get(message_id)
        thread_id = tracked.thread_id if tracked is not None else None
        loop = asyncio.get_running_loop()
        probe_deadline = min(deadline, loop.time() + _GRACEFUL_CANCEL_PROBE_SECONDS)
        while loop.time() < probe_deadline:
            remaining_probe_window = probe_deadline - loop.time()
            if remaining_probe_window <= 0:
                break
            try:
                if await asyncio.wait_for(acancel_run(run_id), timeout=remaining_probe_window):
                    logger.info(
                        "Requested Agno run cancellation after hard task cancel",
                        message_id=message_id,
                        thread_id=thread_id,
                        run_id=run_id,
                    )
                    return "requested"
            except TimeoutError:
                logger.warning(
                    "Agno run cancellation request timed out after hard task cancel",
                    message_id=message_id,
                    thread_id=thread_id,
                    run_id=run_id,
                )
                return "manager_failed"
            except Exception as exc:
                logger.warning(
                    "Agno run cancellation request failed after hard task cancel",
                    message_id=message_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    error=str(exc),
                )
                return "manager_failed"

            await asyncio.sleep(0.05)

        return "not_live"

    async def _graceful_run_cancel_cleanup(self, message_id: str, run_id: str) -> None:
        """Best-effort Agno run cleanup after the response task was already hard-cancelled."""
        tracked = self.tracked_messages.get(message_id)
        thread_id = tracked.thread_id if tracked is not None else None
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.graceful_cancel_fallback_seconds
            outcome = await self._probe_graceful_cancel(message_id, run_id, deadline)

            if outcome == "manager_failed":
                logger.warning(
                    "Agno cancellation manager unavailable after hard task cancel",
                    message_id=message_id,
                    thread_id=thread_id,
                    run_id=run_id,
                )
                return

            if outcome == "not_live":
                logger.warning(
                    "Agno run never became cancellable after hard task cancel",
                    message_id=message_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    cancel_requested=True,
                )
                return

            if outcome != "requested":
                logger.warning(
                    "Unexpected graceful cancellation outcome after hard task cancel",
                    message_id=message_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    outcome=outcome,
                )
                return

            logger.info(
                "Finished graceful Agno cancellation cleanup after hard task cancel",
                message_id=message_id,
                thread_id=thread_id,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Graceful cancellation probe was cancelled after hard task cancel",
                message_id=message_id,
                thread_id=thread_id,
                run_id=run_id,
            )
            raise

    def _schedule_graceful_run_cancel(self, message_id: str, run_id: str) -> None:
        """Queue best-effort Agno run cleanup after the response task is cancelled."""
        self._track_cleanup_task(asyncio.create_task(self._graceful_run_cancel_cleanup(message_id, run_id)))

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
                    logger.info("Removing stop button in cleanup", message_id=message_id, thread_id=tracked.thread_id)
                    try:
                        await client.room_redact(
                            room_id=tracked.room_id,
                            event_id=tracked.reaction_event_id,
                            reason="Response completed",
                        )
                        tracked.reaction_event_id = None
                    except Exception as e:
                        logger.warning(
                            "stop_button_cleanup_failed",
                            message_id=message_id,
                            thread_id=tracked.thread_id,
                            error=str(e),
                        )

            await asyncio.sleep(delay)
            if message_id in self.tracked_messages:
                tracked = self.tracked_messages[message_id]
                logger.info(
                    "Clearing tracked message after delay",
                    message_id=message_id,
                    thread_id=tracked.thread_id,
                    delay=delay,
                )
                del self.tracked_messages[message_id]

        if message_id in self.tracked_messages:
            tracked = self.tracked_messages[message_id]
            logger.info(
                "Scheduling message cleanup",
                message_id=message_id,
                thread_id=tracked.thread_id,
                delay=delay,
                remove_button=remove_button,
            )
            self._track_cleanup_task(asyncio.create_task(delayed_clear()))
        else:
            logger.debug("Message not tracked, skipping cleanup", message_id=message_id)

    async def handle_stop_reaction(self, message_id: str) -> bool:
        """Handle a stop reaction for a message.

        Returns True if hard cancellation was initiated or is already in progress, False otherwise.
        """
        tracked = self.tracked_messages.get(message_id)
        logger.info(
            "Handling stop reaction",
            message_id=message_id,
            thread_id=tracked.thread_id if tracked is not None else None,
            tracked_messages=list(self.tracked_messages.keys()),
        )

        if tracked is not None:
            if tracked.task and not tracked.task.done():
                if tracked.cancel_requested:
                    logger.info(
                        "Cancellation already requested for message",
                        message_id=message_id,
                        thread_id=tracked.thread_id,
                    )
                    return True

                tracked.cancel_requested = True
                logger.info(
                    "Hard cancelling tracked response task",
                    message_id=message_id,
                    thread_id=tracked.thread_id,
                    run_id=tracked.run_id,
                )
                tracked.task.cancel()
                if tracked.run_id:
                    logger.info(
                        "Scheduling best-effort Agno run cleanup after hard task cancel",
                        message_id=message_id,
                        thread_id=tracked.thread_id,
                        run_id=tracked.run_id,
                    )
                    self._schedule_graceful_run_cancel(message_id, tracked.run_id)

                # Don't clear here - let the finally block handle it
                return True
            logger.info(
                "Task already completed or missing",
                message_id=message_id,
                thread_id=tracked.thread_id,
                task_exists=tracked.task is not None,
                task_done=tracked.task.done() if tracked.task else None,
            )
        else:
            logger.warning("Stop reaction for untracked message", message_id=message_id, thread_id=None)
        return False

    async def add_stop_button(self, client: AsyncClient, room_id: str, message_id: str) -> str | None:
        """Add a stop button reaction to a message.

        Returns:
            The event ID of the reaction if successful, None otherwise.

        """
        tracked = self.tracked_messages.get(message_id)
        thread_id = tracked.thread_id if tracked is not None else None
        logger.info("Adding stop button", room_id=room_id, message_id=message_id, thread_id=thread_id)
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
                logger.info(
                    "Stop button added successfully",
                    reaction_event_id=event_id,
                    message_id=message_id,
                    thread_id=thread_id,
                )
                # Update the tracked message with the reaction event ID
                if message_id in self.tracked_messages:
                    self.tracked_messages[message_id].reaction_event_id = event_id
                return event_id
            logger.warning(
                "Failed to add stop button - no event_id in response",
                response=response,
                thread_id=thread_id,
            )
        except Exception as e:
            logger.exception("Exception adding stop button", error=str(e), thread_id=thread_id)
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
                    thread_id=tracked.thread_id,
                    reaction_event_id=tracked.reaction_event_id,
                )
                try:
                    await client.room_redact(
                        room_id=tracked.room_id,
                        event_id=tracked.reaction_event_id,
                        reason="User clicked stop",
                    )
                    tracked.reaction_event_id = None
                    logger.info("Stop button removed successfully", thread_id=tracked.thread_id)
                except Exception as e:
                    logger.exception("Failed to remove stop button", error=str(e), thread_id=tracked.thread_id)
            else:
                logger.debug(
                    "Stop button already removed or missing",
                    message_id=message_id,
                    thread_id=tracked.thread_id,
                    has_reaction_id=tracked.reaction_event_id is not None,
                )
        else:
            logger.debug("Message not tracked, cannot remove stop button", message_id=message_id, thread_id=None)
