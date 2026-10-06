"""Reply span cancellation, hard cancel first and then the Agno run, and the Stop button reaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
from agno.run.cancel import acancel_run

from mindroom.cancellation import request_task_cancel
from mindroom.logging_config import get_logger
from mindroom.matrix.client import send_room_event_result
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nio import AsyncClient

    from mindroom.cancellation import TaskCancelSource

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
class _LiveSpan:
    """The task executing one reply span, and the Agno run it is on."""

    task: asyncio.Task[object]
    run_id: str | None = None
    cancel_requested: bool = False


class SpanRegistry:
    """The task and Agno run of each reply span this bot instance executes.

    A Stop names a span, never a message: the reply's records decide which span
    it reaches, so a reply whose event does not exist yet is just as stoppable.
    """

    def __init__(self, graceful_cancel_fallback_seconds: float = _GRACEFUL_CANCEL_FALLBACK_SECONDS) -> None:
        """Initialize an empty registry."""
        self._spans: dict[str, _LiveSpan] = {}
        # Spans this instance claimed whose task may still register, and the
        # cancellations that reached them first (registration recheck).
        self._expected: set[str] = set()
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
            if span_id in self._expected:
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

    def expect(self, span_id: str) -> None:
        """Note a span this instance is claiming, whose task registers once its claim commits."""
        self._expected.add(span_id)

    def forget(self, span_id: str) -> None:
        """Stop expecting a span: its scope exited, or its claim did not open it."""
        self._expected.discard(span_id)
        self._cancel_on_start.pop(span_id, None)
