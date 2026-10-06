"""Stop button tracking with hard-cancel-first response handling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
from agno.run.cancel import acancel_run

from mindroom.cancellation import request_task_cancel
from mindroom.logging_config import get_logger
from mindroom.matrix.client import send_room_event_result
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from nio import AsyncClient

    from mindroom.cancellation import TaskCancelSource
    from mindroom.message_target import MessageTarget

logger = get_logger(__name__)
_GRACEFUL_CANCEL_FALLBACK_SECONDS = 10.0
_GRACEFUL_CANCEL_PROBE_SECONDS = 0.25


async def _probe_graceful_cancel(run_id: str, deadline: float, log_fields: Mapping[str, object]) -> str:
    """Request Agno run cancellation for one known run during the post-cancel probe window."""
    loop = asyncio.get_running_loop()
    probe_deadline = min(deadline, loop.time() + _GRACEFUL_CANCEL_PROBE_SECONDS)
    while loop.time() < probe_deadline:
        remaining_probe_window = probe_deadline - loop.time()
        if remaining_probe_window <= 0:
            break
        try:
            if await asyncio.wait_for(acancel_run(run_id), timeout=remaining_probe_window):
                logger.info("Requested Agno run cancellation after hard task cancel", run_id=run_id, **log_fields)
                return "requested"
        except TimeoutError:
            logger.warning(
                "Agno run cancellation request timed out after hard task cancel",
                run_id=run_id,
                **log_fields,
            )
            return "manager_failed"
        except Exception as exc:
            logger.warning(
                "Agno run cancellation request failed after hard task cancel",
                run_id=run_id,
                error=str(exc),
                **log_fields,
            )
            return "manager_failed"

        await asyncio.sleep(0.05)

    return "not_live"


async def _graceful_run_cancel_cleanup(
    run_id: str,
    fallback_seconds: float,
    log_fields: Mapping[str, object],
) -> None:
    """Best-effort Agno run cleanup after the response task was already hard-cancelled."""
    try:
        loop = asyncio.get_running_loop()
        outcome = await _probe_graceful_cancel(run_id, loop.time() + fallback_seconds, log_fields)

        if outcome == "manager_failed":
            logger.warning("Agno cancellation manager unavailable after hard task cancel", run_id=run_id, **log_fields)
            return

        if outcome == "not_live":
            logger.warning(
                "Agno run never became cancellable after hard task cancel",
                run_id=run_id,
                cancel_requested=True,
                **log_fields,
            )
            return

        if outcome != "requested":
            logger.warning(
                "Unexpected graceful cancellation outcome after hard task cancel",
                run_id=run_id,
                outcome=outcome,
                **log_fields,
            )
            return

        logger.info(
            "Finished graceful Agno cancellation cleanup after hard task cancel",
            run_id=run_id,
            **log_fields,
        )
    except asyncio.CancelledError:
        logger.warning("Graceful cancellation probe was cancelled after hard task cancel", run_id=run_id, **log_fields)
        raise


async def send_stop_button(client: AsyncClient, room_id: str, message_id: str) -> str | None:
    """Send the Stop button reaction on one message, best effort; return its event ID."""
    try:
        response = await send_room_event_result(
            client,
            room_id,
            "m.reaction",
            build_reaction_content(message_id, "🛑"),
            operation="add_stop_button",
        )
    except Exception as e:
        logger.exception("Exception adding stop button", error=str(e), message_id=message_id, room_id=room_id)
        return None
    if isinstance(response, nio.RoomSendResponse):
        event_id = str(response.event_id)
        logger.info(
            "Stop button added successfully",
            reaction_event_id=event_id,
            message_id=message_id,
            room_id=room_id,
        )
        return event_id
    logger.warning("Failed to add stop button - no event_id in response", response=response, room_id=room_id)
    return None


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

    def can_handle_stop_reaction(self, message_id: str, room_id: str) -> bool:
        """Return whether a stop reaction in ``room_id`` currently has a live semantic consumer."""
        tracked = self._get_active_tracked_message(message_id)
        return tracked is not None and tracked.target.room_id == room_id

    def _schedule_graceful_run_cancel(self, message_id: str, run_id: str) -> None:
        """Queue best-effort Agno run cleanup after the response task is cancelled."""
        tracked = self.tracked_messages.get(message_id)
        log_fields = {"message_id": message_id, **(self._log_target(tracked.target) if tracked is not None else {})}
        self._track_cleanup_task(
            asyncio.create_task(
                _graceful_run_cancel_cleanup(run_id, self.graceful_cancel_fallback_seconds, log_fields),
            ),
        )

    def clear_message(
        self,
        message_id: str,
        client: AsyncClient,
        remove_button: bool = True,
        delay: float = 5.0,
    ) -> None:
        """Clear tracking for a specific message and optionally remove stop button."""
        tracked = self.tracked_messages.get(message_id)
        if tracked is None:
            logger.debug("Message not tracked, skipping cleanup", message_id=message_id)
            return
        reaction_event_id = tracked.reaction_event_id

        async def delayed_clear() -> None:
            """Clear the message and remove stop button after a delay."""
            if remove_button and reaction_event_id:
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
                    if (
                        self.tracked_messages.get(message_id) is tracked
                        and tracked.reaction_event_id == reaction_event_id
                    ):
                        tracked.reaction_event_id = None
                except Exception as e:
                    logger.warning(
                        "stop_button_cleanup_failed",
                        message_id=message_id,
                        error=str(e),
                        **self._log_target(tracked.target),
                    )

            await asyncio.sleep(delay)
            if self.tracked_messages.get(message_id) is tracked:
                logger.info(
                    "Clearing tracked message after delay",
                    message_id=message_id,
                    delay=delay,
                    **self._log_target(tracked.target),
                )
                del self.tracked_messages[message_id]

        logger.info(
            "Scheduling message cleanup",
            message_id=message_id,
            delay=delay,
            remove_button=remove_button,
            **self._log_target(tracked.target),
        )
        self._track_cleanup_task(asyncio.create_task(delayed_clear()))

    def discard_message(self, message_id: str) -> None:
        """Drop process-local tracking without scheduling Matrix cleanup."""
        tracked = self.tracked_messages.pop(message_id, None)
        if tracked is None:
            return
        logger.info(
            "Discarding tracked message for process shutdown",
            message_id=message_id,
            **self._log_target(tracked.target),
        )

    def request_stop_if(self, message_id: str, should_stop: Callable[[], bool]) -> bool:
        """Atomically validate current intent and request cancellation without yielding."""
        if not should_stop():
            return False
        return self._request_stop(message_id)

    def _request_stop(self, message_id: str) -> bool:
        """Request cancellation for the response currently tracked by this message."""
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
                request_task_cancel(tracked.task, cancel_source="user_stop")
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
            logger.debug("Stop reaction for untracked message", message_id=message_id)
        return False

    async def add_stop_button(
        self,
        client: AsyncClient,
        message_id: str,
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
        event_id = await send_stop_button(client, tracked.target.room_id, message_id)
        if event_id is not None:
            tracked.reaction_event_id = event_id
        return event_id

    async def remove_stop_button(
        self,
        client: AsyncClient,
        message_id: str | None = None,
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


@dataclass
class _LiveSpan:
    """The task executing one reply span, and the Agno run it is on."""

    task: asyncio.Task[object]
    run_id: str | None = None
    cancel_requested: bool = False


class SpanRegistry:
    """The task and Agno run of each reply span this bot instance executes (DESIGN.md §8).

    A Stop names a span, never a message: the reply's records decide which span
    it reaches, so a reply whose event does not exist yet is just as stoppable.
    """

    def __init__(self, graceful_cancel_fallback_seconds: float = _GRACEFUL_CANCEL_FALLBACK_SECONDS) -> None:
        """Initialize an empty registry."""
        self._spans: dict[str, _LiveSpan] = {}
        # Cancellations of spans whose task had not registered yet (registration recheck).
        self._cancel_on_start: dict[str, TaskCancelSource | None] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._graceful_cancel_fallback_seconds = graceful_cancel_fallback_seconds

    def register(self, span_id: str, task: asyncio.Task[object]) -> None:
        """Remember the task that executes one span, cancelling it at once if a Stop already reached the span."""
        live = _LiveSpan(task)
        self._spans[span_id] = live

        def forget_finished(_task: asyncio.Task[object]) -> None:
            if self._spans.get(span_id) is live:
                del self._spans[span_id]

        task.add_done_callback(forget_finished)
        if span_id in self._cancel_on_start:
            self._cancel(span_id, live, self._cancel_on_start.pop(span_id))

    def cancel(self, span_id: str, *, cancel_source: TaskCancelSource | None) -> bool:
        """Cancel exactly the named span's task; a span whose task has not registered is cancelled when it does."""
        live = self._spans.get(span_id)
        if live is None:
            self._cancel_on_start[span_id] = cancel_source
            return False
        if live.task.done():
            return False
        self._cancel(span_id, live, cancel_source)
        return True

    def _cancel(self, span_id: str, live: _LiveSpan, cancel_source: TaskCancelSource | None) -> None:
        if live.cancel_requested:
            return
        live.cancel_requested = True
        logger.info("Hard cancelling reply span task", span_id=span_id, run_id=live.run_id, cancel_source=cancel_source)
        request_task_cancel(live.task, cancel_source=cancel_source)
        if live.run_id:
            self._cancel_run(span_id, live.run_id)

    def update_run_id(self, span_id: str, run_id: str | None) -> None:
        """Note the Agno run a span's next attempt uses, cancelling it too if the span was already stopped."""
        live = self._spans.get(span_id)
        if live is None or live.task.done() or live.run_id == run_id:
            return
        live.run_id = run_id
        if live.cancel_requested and run_id:
            self._cancel_run(span_id, run_id)

    def _cancel_run(self, span_id: str, run_id: str) -> None:
        task = asyncio.create_task(
            _graceful_run_cancel_cleanup(run_id, self._graceful_cancel_fallback_seconds, {"span_id": span_id}),
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def live_span_ids(self) -> frozenset[str]:
        """Return the spans whose task is still running."""
        return frozenset(span_id for span_id, live in self._spans.items() if not live.task.done())

    def forget(self, span_id: str) -> None:
        """Drop a cancellation that the span ended before registering its task."""
        self._cancel_on_start.pop(span_id, None)
