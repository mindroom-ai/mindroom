"""Feed hidden events into the ordinary response stream before presentation."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from mindroom.llm.events import ToolCallOrigin, ToolCompleted, ToolStarted
from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from mindroom.tool_system.agent_tool_calls import AgentToolCallStep

__all__ = [
    "CliCallCancelSignal",
    "cli_call_cancel_scope",
    "current_cli_call_cancel",
    "emit_cli_run_event",
    "emit_cli_suspension",
    "emit_cli_tool_event",
    "stream_cli_events",
]

_SINK: ContextVar[Callable[[object], None] | None] = ContextVar("cli_tool_event_sink", default=None)
_CALL_CANCEL: ContextVar[CliCallCancelSignal | None] = ContextVar("cli_call_cancel", default=None)


@dataclass
class CliCallCancelSignal:
    """Route-originated cancel signal for one CLI call, shielding approval publication."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    in_approval: bool = False

    @contextmanager
    def approval_section(self) -> Iterator[None]:
        """Shield approval work from direct task cancellation."""
        self.in_approval = True
        try:
            yield
        finally:
            self.in_approval = False


def current_cli_call_cancel() -> CliCallCancelSignal | None:
    """Return the active CLI call's route-cancel signal, if any."""
    return _CALL_CANCEL.get()


@contextmanager
def cli_call_cancel_scope(signal: CliCallCancelSignal) -> Iterator[None]:
    """Bind one CLI call's route-cancel signal across execution."""
    token = _CALL_CANCEL.set(signal)
    try:
        yield
    finally:
        _CALL_CANCEL.reset(token)


@dataclass(frozen=True)
class _CliSuspension:
    error: BaseException


def emit_cli_suspension(error: BaseException) -> None:
    """Leave the run through the response stream even when its tool runner catches errors."""
    sink = _SINK.get()
    if sink is not None:
        sink(_CliSuspension(error))


def emit_cli_tool_event(event: AgentToolCallStep, parent: str | None) -> None:
    """Publish trusted inner audit events, excluding the canonical outer shell alias."""
    if event.call.tool_call_id == parent:
        return
    cls = ToolStarted if event.kind == "started" else ToolCompleted
    emit_cli_run_event(cls(event.call), parent=parent, toolkit_name=event.key.toolkit)


def emit_cli_run_event(
    event: ToolStarted | ToolCompleted,
    *,
    parent: str | None,
    toolkit_name: str | None,
) -> None:
    """Retain native event provenance while adding trusted hidden-call audit identity."""
    sink = _SINK.get()
    if sink is None:
        return
    sink(replace(event, origin=ToolCallOrigin(parent, toolkit_name)))


async def stream_cli_events[EventT](
    source: AsyncIterator[EventT],
) -> AsyncIterator[EventT | ToolStarted | ToolCompleted]:
    """Merge the run's own events with the hidden calls' tool events, which may start before tool preparation.

    The existing attempt and presentation consumers remain the only trace owners.
    """

    async def produce(on_event: Callable[[object], None]) -> None:
        token = _SINK.set(on_event)
        try:
            async with closing_async_stream(source):
                async for event in source:
                    on_event(event)
        finally:
            _SINK.reset(token)

    events = callback_event_stream(produce)
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, _CliSuspension):
                raise event.error
            if event is not None:
                # The sink receives only the source's events and the hidden calls' ToolStarted and ToolCompleted.
                yield cast("EventT | ToolStarted | ToolCompleted", event)
