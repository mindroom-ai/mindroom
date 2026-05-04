"""Stop button tracking with hard-cancel-first response handling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
from agno.run.cancel import acancel_run

from mindroom.cancellation import USER_STOP_CANCEL_MSG, request_task_cancel
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from nio import AsyncClient

    from mindroom.message_target import MessageTarget

logger = get_logger(__name__)
_GRACEFUL_CANCEL_FALLBACK_SECONDS = 10.0
_GRACEFUL_CANCEL_PROBE_SECONDS = 0.25


@dataclass
class _TrackedMessage:
    """Track a message with stop button."""

    message_id: str
    target: MessageTarget
    task: asyncio.Task[None]
    reaction_event_id: str | None = None
    run_id: str | None = None
    cancel_requested: bool = False


class StopManager:
    """Manage stop reactions with immediate task cancellation."""

    def __init__(self, graceful_cancel_fallback_seconds: float = _GRACEFUL_CANCEL_FALLBACK_SECONDS) -> None:
        """Initialize the stop manager."""
        self.tracked_messages: dict[str, _TrackedMessage] = {}
        self.cleanup_tasks: list[asyncio.Task[None]] = []
        self.graceful_cancel_fallback_seconds = graceful_cancel_fallback_seconds
        logger.info("StopManager initialized")

    @staticmethod
    def _log_target(target: MessageTarget) -> dict[str, str | None]:
        """Return standard room/thread fields for tracked-message logs."""
        return {
            "room_id": target.room_id,
            "thread_id": target.resolved_thread_id,
        }

    def set_current(
        self,
        message_id: str,
        target: MessageTarget,
        task: asyncio.Task[None],
        reaction_event_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Track a message generation."""
        self.tracked_messages[message_id] = _TrackedMessage(
            message_id=message_id,
            target=target,
            task=task,
            reaction_event_id=reaction_event_id,
            run_id=run_id,
        )
        logger.info(
            "Tracking message generation",
            message_id=message_id,
            reaction_event_id=reaction_event_id,
            run_id=run_id,
            total_tracked=len(self.tracked_messages),
            **self._log_target(target),
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
            previous_run_id=previous_run_id,
            run_id=run_id,
            cancel_requested=tracked.cancel_requested,
            **self._log_target(tracked.target),
        )

        if tracked.cancel_requested and run_id:
            logger.info(
                "Stop already requested; scheduling best-effort cleanup for updated run id",
                message_id=message_id,
                run_id=run_id,
                **self._log_target(tracked.target),
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

    def get_tracked_target(self, message_id: str) -> MessageTarget | None:
        """Return the tracked delivery target for one message when available."""
        tracked = self.tracked_messages.get(message_id)
        if tracked is None:
            return None
        return tracked.target

    async def _probe_graceful_cancel(self, message_id: str, run_id: str, deadline: float) -> str:
        """Request Agno run cancellation for one known run during the post-cancel probe window."""
        tracked = self.tracked_messages.get(message_id)
        target_log = self._log_target(tracked.target) if tracked is not None else {}
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
                        run_id=run_id,
                        **target_log,
                    )
                    return "requested"
            except TimeoutError:
                logger.warning(
                    "Agno run cancellation request timed out after hard task cancel",
                    message_id=message_id,
                    run_id=run_id,
                    **target_log,
                )
                return "manager_failed"
            except Exception as exc:
                logger.warning(
                    "Agno run cancellation request failed after hard task cancel",
                    message_id=message_id,
                    run_id=run_id,
                    error=str(exc),
                    **target_log,
                )
                return "manager_failed"

            await asyncio.sleep(0.05)

        return "not_live"

    async def _graceful_run_cancel_cleanup(self, message_id: str, run_id: str) -> None:
        """Best-effort Agno run cleanup after the response task was already hard-cancelled."""
        tracked = self.tracked_messages.get(message_id)
        target_log = self._log_target(tracked.target) if tracked is not None else {}
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.graceful_cancel_fallback_seconds
            outcome = await self._probe_graceful_cancel(message_id, run_id, deadline)

            if outcome == "manager_failed":
                logger.warning(
                    "Agno cancellation manager unavailable after hard task cancel",
                    message_id=message_id,
                    run_id=run_id,
                    **target_log,
                )
                return

            if outcome == "not_live":
                logger.warning(
                    "Agno run never became cancellable after hard task cancel",
                    message_id=message_id,
                    run_id=run_id,
                    cancel_requested=True,
                    **target_log,
                )
                return

            if outcome != "requested":
                logger.warning(
                    "Unexpected graceful cancellation outcome after hard task cancel",
                    message_id=message_id,
                    run_id=run_id,
                    outcome=outcome,
                    **target_log,
                )
                return

            logger.info(
                "Finished graceful Agno cancellation cleanup after hard task cancel",
                message_id=message_id,
                run_id=run_id,
                **target_log,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Graceful cancellation probe was cancelled after hard task cancel",
                message_id=message_id,
                run_id=run_id,
                **target_log,
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
        notify_outbound_redaction: Callable[[str, str], None] | None = None,
    ) -> None:
        """Clear tracking for a specific message and optionally remove stop button."""

        async def delayed_clear() -> None:
            """Clear the message and remove stop button after a delay."""
            if remove_button and message_id in self.tracked_messages:
                tracked = self.tracked_messages[message_id]
                if tracked.reaction_event_id:
                    reaction_event_id = tracked.reaction_event_id
                    logger.info(
                        "Removing stop button in cleanup",
                        message_id=message_id,
                        **self._log_target(tracked.target),
                    )
                    try:
                        await client.room_redact(
                            room_id=tracked.target.room_id,
                            event_id=reaction_event_id,
                            reason="Response completed",
                        )
                        tracked.reaction_event_id = None
                        if notify_outbound_redaction is not None:
                            notify_outbound_redaction(tracked.target.room_id, reaction_event_id)
                    except Exception as e:
                        logger.warning(
                            "stop_button_cleanup_failed",
                            message_id=message_id,
                            error=str(e),
                            **self._log_target(tracked.target),
                        )

            await asyncio.sleep(delay)
            if message_id in self.tracked_messages:
                tracked = self.tracked_messages[message_id]
                logger.info(
                    "Clearing tracked message after delay",
                    message_id=message_id,
                    delay=delay,
                    **self._log_target(tracked.target),
                )
                del self.tracked_messages[message_id]

        if message_id in self.tracked_messages:
            tracked = self.tracked_messages[message_id]
            logger.info(
                "Scheduling message cleanup",
                message_id=message_id,
                delay=delay,
                remove_button=remove_button,
                **self._log_target(tracked.target),
            )
            self._track_cleanup_task(asyncio.create_task(delayed_clear()))
        else:
            logger.debug("Message not tracked, skipping cleanup", message_id=message_id)

    async def handle_stop_reaction(self, message_id: str) -> bool:
        """Handle a stop reaction for a message.

        Returns True if hard cancellation was initiated or is already in progress, False otherwise.
        """
        tracked = self.tracked_messages.get(message_id)
        target_log = self._log_target(tracked.target) if tracked is not None else {}
        logger.info(
            "Handling stop reaction",
            message_id=message_id,
            tracked_messages=list(self.tracked_messages.keys()),
            **target_log,
        )

        if tracked is not None:
            if tracked.task and not tracked.task.done():
                if tracked.cancel_requested:
                    logger.info(
                        "Cancellation already requested for message",
                        message_id=message_id,
                        **target_log,
                    )
                    return True

                tracked.cancel_requested = True
                logger.info(
                    "Hard cancelling tracked response task",
                    message_id=message_id,
                    run_id=tracked.run_id,
                    **target_log,
                )
                request_task_cancel(tracked.task, cancel_msg=USER_STOP_CANCEL_MSG)
                if tracked.run_id:
                    logger.info(
                        "Scheduling best-effort Agno run cleanup after hard task cancel",
                        message_id=message_id,
                        run_id=tracked.run_id,
                        **target_log,
                    )
                    self._schedule_graceful_run_cancel(message_id, tracked.run_id)
                return True
            logger.info(
                "Task already completed or missing",
                message_id=message_id,
                task_exists=tracked.task is not None,
                task_done=tracked.task.done() if tracked.task else None,
                **target_log,
            )
        else:
            logger.warning("Stop reaction for untracked message", message_id=message_id)
        return False

    async def add_stop_button(
        self,
        client: AsyncClient,
        message_id: str,
        *,
        ignore_unverified_devices: bool = False,
        notify_outbound_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> str | None:
        """Add a stop button reaction to a tracked message."""
        tracked = self.tracked_messages.get(message_id)
        if tracked is None:
            logger.warning("Cannot add stop button for untracked message", message_id=message_id)
            return None

        logger.info(
            "Adding stop button",
            message_id=message_id,
            **self._log_target(tracked.target),
        )
        try:
            response = await client.room_send(
                room_id=tracked.target.room_id,
                message_type="m.reaction",
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": message_id,
                        "key": "🛑",
                    },
                },
                ignore_unverified_devices=ignore_unverified_devices,
            )
            if isinstance(response, nio.RoomSendResponse):
                event_id = str(response.event_id)
                logger.info(
                    "Stop button added successfully",
                    reaction_event_id=event_id,
                    message_id=message_id,
                    **self._log_target(tracked.target),
                )
                tracked.reaction_event_id = event_id
                if notify_outbound_event is not None:
                    notify_outbound_event(
                        tracked.target.room_id,
                        {
                            "type": "m.reaction",
                            "room_id": tracked.target.room_id,
                            "event_id": event_id,
                            "sender": client.user_id if isinstance(client.user_id, str) else None,
                            "content": {
                                "m.relates_to": {
                                    "rel_type": "m.annotation",
                                    "event_id": message_id,
                                    "key": "🛑",
                                },
                            },
                        },
                    )
                return event_id
            logger.warning(
                "Failed to add stop button - no event_id in response",
                response=response,
                **self._log_target(tracked.target),
            )
        except Exception as e:
            logger.exception(
                "Exception adding stop button",
                error=str(e),
                **self._log_target(tracked.target),
            )
        return None

    async def remove_stop_button(
        self,
        client: AsyncClient,
        message_id: str | None = None,
        *,
        notify_outbound_redaction: Callable[[str, str], None] | None = None,
    ) -> None:
        """Remove the stop button reaction immediately when user clicks it."""
        if message_id and message_id in self.tracked_messages:
            tracked = self.tracked_messages[message_id]
            if tracked.reaction_event_id:
                reaction_event_id = tracked.reaction_event_id
                logger.info(
                    "Removing stop button immediately (user clicked)",
                    message_id=message_id,
                    reaction_event_id=reaction_event_id,
                    **self._log_target(tracked.target),
                )
                try:
                    await client.room_redact(
                        room_id=tracked.target.room_id,
                        event_id=reaction_event_id,
                        reason="User clicked stop",
                    )
                    tracked.reaction_event_id = None
                    if notify_outbound_redaction is not None:
                        notify_outbound_redaction(tracked.target.room_id, reaction_event_id)
                    logger.info("Stop button removed successfully", **self._log_target(tracked.target))
                except Exception as e:
                    logger.exception(
                        "Failed to remove stop button",
                        error=str(e),
                        **self._log_target(tracked.target),
                    )
            else:
                logger.debug(
                    "Stop button already removed or missing",
                    message_id=message_id,
                    has_reaction_id=tracked.reaction_event_id is not None,
                    **self._log_target(tracked.target),
                )
        else:
            logger.debug("Message not tracked, cannot remove stop button", message_id=message_id)
