"""Reply span cancellation and the Stop button reaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.cancellation import request_task_cancel
from mindroom.logging_config import get_logger
from mindroom.matrix.client import send_room_event_result
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    import asyncio

    from nio import AsyncClient

    from mindroom.cancellation import TaskCancelSource

logger = get_logger(__name__)


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
    """The task executing one reply span."""

    task: asyncio.Task[object]
    cancel_requested: bool = False


class SpanRegistry:
    """The task of each reply span this bot instance executes.

    A Stop names a span, never a message: the reply's records decide which span
    it reaches, so a reply whose event does not exist yet is just as stoppable.
    Cancelling the task is the whole stop: every run lives inside its reply's
    cancellable task and stores its terminal record before the cancellation leaves it.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._spans: dict[str, _LiveSpan] = {}
        # Spans this instance claimed whose task may still register, and the
        # cancellations that reached them first (registration recheck).
        self._expected: set[str] = set()
        self._cancel_on_start: dict[str, TaskCancelSource | None] = {}

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
        logger.info("Hard cancelling reply span task", span_id=span_id, cancel_source=cancel_source)
        request_task_cancel(live.task, cancel_source=cancel_source)

    def live_span_ids(self) -> frozenset[str]:
        """Return the spans whose task is still running."""
        return frozenset(span_id for span_id, live in self._spans.items() if not live.task.done())

    def claimed_span_ids(self) -> frozenset[str]:
        """Return the spans whose task runs, or registers once its claim commits."""
        return self.live_span_ids() | self._expected

    def expect(self, span_id: str) -> None:
        """Note a span this instance is claiming, whose task registers once its claim commits."""
        self._expected.add(span_id)

    def forget(self, span_id: str) -> None:
        """Stop expecting a span: its scope exited, or its claim did not open it."""
        self._expected.discard(span_id)
        self._cancel_on_start.pop(span_id, None)
